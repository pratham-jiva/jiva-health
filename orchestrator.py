"""
Jiva Health Orchestrator
Điều phối 8-step consultation workflow.
Jiva tự điều phối, không dùng Team-Z SDK.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic

from agents import consultant, research, evaluator, causes, solutions

# --- Config ---
MODEL = "claude-sonnet-4-20250514"
REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

EMERGENCY_KEYWORDS = [
    "đau ngực", "khó thở", "mất ý thức", "chảy máu nặng",
    "sốt cao", "co giật", "chest pain", "difficulty breathing",
    "unconscious", "heavy bleeding", "high fever", "seizure",
    "đột quỵ", "stroke", "heart attack", "nhồi máu"
]

DISCLAIMER_VN = """⚠️ THÔNG BÁO: Đây chỉ là thông tin tham khảo, KHÔNG phải chẩn đoán y khoa.
Vui lòng tham khảo ý kiến bác sĩ cho mọi quyết định điều trị.
KHẨN CẤP: Gọi 115 (VN) / 911 (US)"""

DISCLAIMER_EN = """⚠️ DISCLAIMER: This is for informational purposes only, NOT medical diagnosis.
Please consult your doctor for all treatment decisions.
EMERGENCY: Call 115 (VN) / 911 (US)"""


class HealthOrchestrator:
    """Orchestrates the 8-step health consultation workflow."""

    def __init__(self):
        self.client = anthropic.Anthropic()
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

        # Step 2: Research FIRST
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

        # Step 4.5: Treatment Eval (included in eval step)

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
                    "⚠️ CẢNH BÁO KHẨN CẤP\n\n"
                    f"Phát hiện triệu chứng khẩn cấp: {', '.join(found)}\n\n"
                    "GỌI NGAY:\n"
                    "🇻🇳 115 (Cấp cứu VN)\n"
                    "🇺🇸 911 (Emergency US)\n\n"
                    "Đây có thể là tình huống cần cấp cứu. "
                    "Vui lòng đến cơ sở y tế gần nhất NGAY."
                ),
            }
        return {"is_emergency": False}

    def _quick_intake(self, message: str) -> dict:
        """Step 1: Extract patient profile from message."""
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=consultant.SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Bệnh nhân gửi tin nhắn sau. "
                        f"Trích xuất Patient Profile từ thông tin có sẵn. "
                        f"Những gì chưa biết thì ghi 'chưa rõ'.\n\n"
                        f"Tin nhắn: {message}\n\n"
                        f"Output YAML patient_profile."
                    ),
                }
            ],
        )
        return {"raw_message": message, "extracted": response.content[0].text}

    def _run_research(self, profile: dict) -> str:
        """Step 2: Research medical literature."""
        messages = research.build_research_prompt(profile)
        system_msg = messages[0]["content"]
        user_msgs = messages[1:]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system_msg,
            messages=user_msgs,
        )
        return response.content[0].text

    def _run_eval(self, profile: dict, research_result: str) -> str:
        """Step 3 & 4.5: Status Assessment + Treatment ABCEF."""
        messages = evaluator.build_eval_prompt(profile, research_result)
        system_msg = messages[0]["content"]
        user_msgs = messages[1:]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system_msg,
            messages=user_msgs,
        )
        return response.content[0].text

    def _run_causes(self, profile: dict, research_result: str) -> str:
        """Step 4: Causal chain analysis."""
        messages = causes.build_causes_prompt(profile, research_result)
        system_msg = messages[0]["content"]
        user_msgs = messages[1:]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=3000,
            system=system_msg,
            messages=user_msgs,
        )
        return response.content[0].text

    def _run_solutions(
        self, profile: dict, research_result: str,
        eval_result: str, causes_result: str
    ) -> str:
        """Step 5: Treatment options."""
        messages = solutions.build_solutions_prompt(
            profile, research_result, eval_result, causes_result
        )
        system_msg = messages[0]["content"]
        user_msgs = messages[1:]

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system_msg,
            messages=user_msgs,
        )
        return response.content[0].text

    def _synthesize_report(self) -> str:
        """Step 6: Generate bilingual consultation report."""
        c = self.consultation

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=6000,
            system=(
                "Bạn là Chairman - tổng hợp báo cáo tư vấn y khoa. "
                "Tạo báo cáo song ngữ Việt-Anh, rõ ràng, có cấu trúc. "
                "LUÔN có disclaimer đầu và cuối."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"""Tổng hợp báo cáo từ kết quả các agent:

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

Tạo báo cáo theo template:

{DISCLAIMER_VN}

# BÁO CÁO TƯ VẤN Y KHOA / MEDICAL CONSULTATION REPORT

## 1. TỔNG QUAN / OVERVIEW
## 2. PHÂN TÍCH NGUYÊN NHÂN / CAUSAL ANALYSIS
## 3. THÔNG TIN Y KHOA / MEDICAL INFO
## 4. PHƯƠNG ÁN THAM KHẢO / RECOMMENDATIONS
## 5. ĐÁNH GIÁ PHÁC ĐỒ (ABCEF) / TREATMENT EVALUATION
## 6. HƯỚNG DẪN CHĂM SÓC / CARE GUIDE

{DISCLAIMER_VN}""",
                }
            ],
        )
        return response.content[0].text

    def _generate_handoff(self) -> str:
        """Step 7: Handoff summary & teach-back questions."""
        c = self.consultation

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=(
                "Bạn là Consultant đang trao trả bệnh nhân. "
                "Tóm tắt ngắn gọn, ấm áp, dễ hiểu. "
                "BẮT BUỘC có 2-3 câu teach-back."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"""Tạo handoff cho bệnh nhân dựa trên báo cáo:

{c['report'][:3000]}

Format:
- 3-5 điểm chính cần nhớ
- Thuốc: cách dùng + tác dụng phụ cần biết
- Sinh hoạt: ăn uống, nghỉ ngơi, vận động
- Tránh: kiêng gì
- Dấu hiệu cần gặp BS NGAY
- 2-3 câu teach-back (hỏi để kiểm tra bệnh nhân hiểu)
- Nhắc lịch tái khám
- Chúc khỏe""",
                }
            ],
        )
        return response.content[0].text

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
        msg = input("Mô tả triệu chứng/tình trạng: ")

    print("\nĐang tư vấn...\n")
    result = run_consultation(msg)

    if result.get("emergency"):
        print(result["message"])
    else:
        print(result["report"])
        print("\n---\n")
        print(result["handoff"])
        print(f"\nBáo cáo đã lưu: {result['report_path']}")
