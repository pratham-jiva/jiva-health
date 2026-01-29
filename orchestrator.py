"""
Jiva Health Orchestrator
Dieu phoi 8-step consultation workflow.
Dung Claude Code CLI (subprocess) thay vi Anthropic API.
"""

import json
import os
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

from agents import consultant, research, evaluator, causes, solutions

# --- Config ---
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
MODEL = "sonnet"  # Claude Code model alias
REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

EMERGENCY_KEYWORDS = [
    "dau nguc", "kho tho", "mat y thuc", "chay mau nang",
    "sot cao", "co giat", "chest pain", "difficulty breathing",
    "unconscious", "heavy bleeding", "high fever", "seizure",
    "dot quy", "stroke", "heart attack", "nhoi mau",
    "đau ngực", "khó thở", "mất ý thức", "chảy máu nặng",
    "sốt cao", "co giật", "đột quỵ", "nhồi máu"
]

DISCLAIMER_VN = """THONG BAO: Day chi la thong tin tham khao, KHONG phai chan doan y khoa.
Vui long tham khao y kien bac si cho moi quyet dinh dieu tri.
KHAN CAP: Goi 115 (VN) / 911 (US)"""

DISCLAIMER_EN = """DISCLAIMER: This is for informational purposes only, NOT medical diagnosis.
Please consult your doctor for all treatment decisions.
EMERGENCY: Call 115 (VN) / 911 (US)"""


def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 4000) -> str:
    """Call Claude Code CLI in print mode.

    Uses: claude -p "prompt" --model sonnet --no-session-persistence
    System prompt is prepended to user prompt since CLI has no --system flag.
    """
    full_prompt = f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"

    cmd = [
        CLAUDE_BIN,
        "-p", full_prompt,
        "--model", MODEL,
        "--no-session-persistence",
        "--output-format", "text",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr[:500]}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI timed out (120s)")


class HealthOrchestrator:
    """Orchestrates the 8-step health consultation workflow."""

    def __init__(self):
        self.consultation = {
            "id": None,
            "started_at": None,
            "patient_profile": None,
            "research_findings": None,
            "status_assessment": None,
            "treatment_eval": None,
            "causal_analysis": None,
            "solutions": None,
            "report": None,
            "handoff": None,
        }

    def start_consultation(self, patient_message: str) -> dict:
        """Run full 8-step consultation from patient message."""
        consultation_id = f"c_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.consultation["id"] = consultation_id
        self.consultation["started_at"] = datetime.now().isoformat()

        steps = {}

        # Step 0: Emergency Check
        emergency = self._check_emergency(patient_message)
        if emergency["is_emergency"]:
            return {
                "id": consultation_id,
                "emergency": True,
                "message": emergency["message"],
                "steps_completed": ["emergency_check"],
            }
        steps["emergency_check"] = "passed"

        # Step 1: Quick Intake (from message directly, no interview)
        profile = self._quick_intake(patient_message)
        self.consultation["patient_profile"] = profile
        steps["intake"] = "done"

        # Step 2: Research
        research_result = self._run_research(profile)
        self.consultation["research_findings"] = research_result
        steps["research"] = "done"

        # Step 3: Status Assessment
        eval_result = self._run_eval(profile, research_result)
        self.consultation["status_assessment"] = eval_result
        steps["eval"] = "done"

        # Step 4: Cause Analysis
        causes_result = self._run_causes(profile, research_result)
        self.consultation["causal_analysis"] = causes_result
        steps["causes"] = "done"

        # Step 5: Solutions
        solutions_result = self._run_solutions(
            profile, research_result, eval_result, causes_result
        )
        self.consultation["solutions"] = solutions_result
        steps["solutions"] = "done"

        # Step 6: Synthesis - Generate Report
        report = self._synthesize_report()
        self.consultation["report"] = report
        steps["synthesis"] = "done"

        # Step 7: Handoff - Summary & Teach-back
        handoff = self._generate_handoff()
        self.consultation["handoff"] = handoff
        steps["handoff"] = "done"

        # Save report
        report_path = self._save_report(consultation_id, report)

        return {
            "id": consultation_id,
            "emergency": False,
            "steps_completed": list(steps.keys()),
            "report": report,
            "handoff": handoff,
            "report_path": str(report_path),
        }

    def _check_emergency(self, message: str) -> dict:
        """Step 0: Emergency keyword check."""
        message_lower = message.lower()
        found = [kw for kw in EMERGENCY_KEYWORDS if kw in message_lower]

        if found:
            return {
                "is_emergency": True,
                "keywords": found,
                "message": (
                    "CANH BAO KHAN CAP\n\n"
                    f"Phat hien trieu chung khan cap: {', '.join(found)}\n\n"
                    "GOI NGAY:\n"
                    "VN: 115 (Cap cuu)\n"
                    "US: 911 (Emergency)\n\n"
                    "Day co the la tinh huong can cap cuu. "
                    "Vui long den co so y te gan nhat NGAY."
                ),
            }
        return {"is_emergency": False}

    def _quick_intake(self, message: str) -> dict:
        """Step 1: Extract patient profile from message."""
        response = _call_claude(
            system_prompt=consultant.SYSTEM_PROMPT,
            user_prompt=(
                f"Benh nhan gui tin nhan sau. "
                f"Trich xuat Patient Profile tu thong tin co san. "
                f"Nhung gi chua biet thi ghi 'chua ro'.\n\n"
                f"Tin nhan: {message}\n\n"
                f"Output YAML patient_profile."
            ),
        )
        return {"raw_message": message, "extracted": response}

    def _run_research(self, profile: dict) -> str:
        """Step 2: Research medical literature."""
        messages = research.build_research_prompt(profile)
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]
        return _call_claude(system_prompt=system_msg, user_prompt=user_msg)

    def _run_eval(self, profile: dict, research_result: str) -> str:
        """Step 3 & 4.5: Status Assessment + Treatment ABCEF."""
        messages = evaluator.build_eval_prompt(profile, research_result)
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]
        return _call_claude(system_prompt=system_msg, user_prompt=user_msg)

    def _run_causes(self, profile: dict, research_result: str) -> str:
        """Step 4: Causal chain analysis."""
        messages = causes.build_causes_prompt(profile, research_result)
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]
        return _call_claude(system_prompt=system_msg, user_prompt=user_msg)

    def _run_solutions(
        self, profile: dict, research_result: str,
        eval_result: str, causes_result: str
    ) -> str:
        """Step 5: Treatment options."""
        messages = solutions.build_solutions_prompt(
            profile, research_result, eval_result, causes_result
        )
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]
        return _call_claude(system_prompt=system_msg, user_prompt=user_msg)

    def _synthesize_report(self) -> str:
        """Step 6: Generate bilingual consultation report."""
        c = self.consultation

        return _call_claude(
            system_prompt=(
                "Ban la Chairman - tong hop bao cao tu van y khoa. "
                "Tao bao cao song ngu Viet-Anh, ro rang, co cau truc. "
                "LUON co disclaimer dau va cuoi."
            ),
            user_prompt=f"""Tong hop bao cao tu ket qua cac agent:

## Patient Profile
{c['patient_profile'].get('extracted', '')}

## Research Findings
{c['research_findings']}

## Status Assessment & Treatment Eval
{c['status_assessment']}

## Causal Analysis
{c['causal_analysis']}

## Solutions
{c['solutions']}

Tao bao cao theo template:

{DISCLAIMER_VN}

# BAO CAO TU VAN Y KHOA / MEDICAL CONSULTATION REPORT

## 1. TONG QUAN / OVERVIEW
## 2. PHAN TICH NGUYEN NHAN / CAUSAL ANALYSIS
## 3. THONG TIN Y KHOA / MEDICAL INFO
## 4. PHUONG AN THAM KHAO / RECOMMENDATIONS
## 5. DANH GIA PHAC DO (ABCEF) / TREATMENT EVALUATION
## 6. HUONG DAN CHAM SOC / CARE GUIDE

{DISCLAIMER_VN}""",
        )

    def _generate_handoff(self) -> str:
        """Step 7: Handoff summary & teach-back questions."""
        c = self.consultation

        return _call_claude(
            system_prompt=(
                "Ban la Consultant dang trao tra benh nhan. "
                "Tom tat ngan gon, am ap, de hieu. "
                "BAT BUOC co 2-3 cau teach-back."
            ),
            user_prompt=f"""Tao handoff cho benh nhan dua tren bao cao:

{c['report'][:3000]}

Format:
- 3-5 diem chinh can nho
- Thuoc: cach dung + tac dung phu can biet
- Sinh hoat: an uong, nghi ngoi, van dong
- Tranh: kieng gi
- Dau hieu can gap BS NGAY
- 2-3 cau teach-back (hoi de kiem tra benh nhan hieu)
- Nhac lich tai kham
- Chuc khoe""",
        )

    def _save_report(self, consultation_id: str, report: str) -> Path:
        """Save report to file."""
        report_path = REPORTS_DIR / f"{consultation_id}.md"
        report_path.write_text(
            f"<!-- Consultation: {consultation_id} -->\n"
            f"<!-- Generated: {datetime.now().isoformat()} -->\n\n"
            f"{report}",
            encoding="utf-8",
        )
        return report_path


# --- Convenience ---

def run_consultation(message: str) -> dict:
    """Quick function to run a full consultation."""
    orchestrator = HealthOrchestrator()
    return orchestrator.start_consultation(message)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = input("Mo ta trieu chung/tinh trang: ")

    print("\nDang tu van...\n")
    result = run_consultation(msg)

    if result.get("emergency"):
        print(result["message"])
    else:
        print(result["report"])
        print("\n---\n")
        print(result["handoff"])
        print(f"\nBao cao da luu: {result['report_path']}")
