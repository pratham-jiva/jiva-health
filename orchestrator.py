"""
Jiva Health Orchestrator
Dieu phoi 8-step consultation workflow.
Dung Claude Code CLI (subprocess) thay vi Anthropic API.
Luu tat ca vao SQLite database.
"""

import json
import os
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

from agents import consultant, research, evaluator, causes, solutions
import database as db

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
    "dau nguc", "kho tho", "mat y thuc", "chay mau nang",
    "sot cao", "co giat", "dot quy", "nhoi mau"
]

DISCLAIMER_VN = """THÔNG BÁO: Đây chỉ là thông tin tham khảo, KHÔNG phải chẩn đoán y khoa.
Vui lòng tham khảo ý kiến bác sĩ cho mọi quyết định điều trị.
KHẨN CẤP: Gọi 115 (VN) / 911 (US)"""

DISCLAIMER_EN = """DISCLAIMER: This is for informational purposes only, NOT medical diagnosis.
Please consult your doctor for all treatment decisions.
EMERGENCY: Call 115 (VN) / 911 (US)"""


NEED_MORE_INFO_TAG = "[NEED_MORE_INFO]"


def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 4000) -> str:
    """Call Claude Code CLI in print mode.

    System prompt includes instruction: if more info needed from patient,
    output [NEED_MORE_INFO] followed by the question. This replaces
    AskUserQuestion which only works in interactive terminal.
    """
    extra_instruction = (
        "\n\n## QUAN TRONG - Tuong tac voi benh nhan\n"
        "Ban dang chay trong che do tu dong (KHONG co terminal).\n"
        "- KHONG BAO GIO dung tool AskUserQuestion.\n"
        "- Neu can hoi them benh nhan de phan tich tot hon, "
        f"bat dau response voi {NEED_MORE_INFO_TAG} roi ghi cau hoi.\n"
        f"Vi du: {NEED_MORE_INFO_TAG} Ban co the cho biet ket qua xet nghiem gan day?\n"
        "- Neu KHONG can hoi them, phan tich binh thuong."
    )

    full_prompt = f"[SYSTEM]\n{system_prompt}{extra_instruction}\n\n[USER]\n{user_prompt}"

    cmd = [
        CLAUDE_BIN,
        "-p", full_prompt,
        "--model", MODEL,
        "--no-session-persistence",
        "--output-format", "text",
        "--allowedTools", "",  # No interactive tools allowed
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr[:500]}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI timed out (300s)")


class HealthOrchestrator:
    """Orchestrates the 8-step health consultation workflow."""

    # Step descriptions for progress notifications
    STEP_NAMES = {
        "emergency_check": "Kiểm tra khẩn cấp",
        "intake": "Thu thập thông tin bệnh nhân",
        "interview": "Hỏi thêm thông tin",
        "research": "Tra cứu tài liệu y khoa",
        "eval": "Đánh giá tình trạng",
        "causes": "Phân tích nguyên nhân",
        "solutions": "Tìm phương án điều trị",
        "synthesis": "Tổng hợp báo cáo",
        "handoff": "Chuẩn bị kết quả tư vấn",
    }

    MAX_INTERVIEW_TURNS = 3  # Max follow-up questions before proceeding

    def __init__(self, user_id: int = None, patient_id: int = None,
                 on_step_start=None, on_step_done=None):
        self.user_id = user_id
        self.patient_id = patient_id
        self.on_step_start = on_step_start  # callback(step_name, step_desc)
        self.on_step_done = on_step_done    # callback(step_name, step_desc)
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
        self.interview_history = []  # conversation turns for intake
        self.interview_turn = 0
        self._analysis_resume_step = "research"  # for resuming after clarification
        self._analysis_pending_question_step = None

    def _notify(self, step: str, event: str = "start"):
        """Notify progress via callback."""
        desc = self.STEP_NAMES.get(step, step)
        try:
            if event == "start" and self.on_step_start:
                self.on_step_start(step, desc)
            elif event == "done" and self.on_step_done:
                self.on_step_done(step, desc)
        except Exception:
            pass  # Don't let notification errors break the workflow

    def start_intake(self, patient_message: str) -> dict:
        """Phase 1: Emergency check + first interview question.

        Returns:
            dict with keys:
                - "emergency": True if emergency detected
                - "needs_interview": True if bot should ask user follow-up
                - "question": the follow-up question to ask user
                - "consultation_id": for tracking
                - Or full result if interview not needed
        """
        consultation_id = f"c_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.consultation["id"] = consultation_id
        self.consultation["started_at"] = datetime.now().isoformat()

        # Create consultation record in DB
        if self.user_id:
            self._db_consultation = db.create_consultation(
                consultation_id=consultation_id,
                user_id=self.user_id,
                patient_id=self.patient_id,
                raw_message=patient_message,
            )
            db.save_message(
                user_id=self.user_id,
                role="user",
                content=patient_message,
                consultation_id=self._db_consultation["id"],
            )
        else:
            self._db_consultation = None

        # Step 0: Emergency Check
        self._notify("emergency_check", "start")
        emergency = self._check_emergency(patient_message)
        if emergency["is_emergency"]:
            if self._db_consultation:
                db.update_consultation(
                    consultation_id,
                    is_emergency=1,
                    status="emergency",
                    completed_at=datetime.now().isoformat(),
                )
            return {
                "id": consultation_id,
                "emergency": True,
                "message": emergency["message"],
            }
        self._notify("emergency_check", "done")

        # Step 1: Interview - ask Claude to interview the patient
        self._notify("interview", "start")
        self.interview_history = [{"role": "user", "content": patient_message}]
        self.interview_turn = 0

        question = self._interview_step(patient_message)

        if question:
            # Claude wants to ask follow-up questions
            self.interview_history.append({"role": "assistant", "content": question})
            if self._db_consultation:
                db.save_message(
                    user_id=self.user_id,
                    role="assistant",
                    content=question,
                    consultation_id=self._db_consultation["id"],
                )
            return {
                "id": consultation_id,
                "emergency": False,
                "needs_interview": True,
                "question": question,
                "consultation_id": consultation_id,
            }
        else:
            # Enough info from first message - proceed directly
            return self._run_analysis_phase()

    def continue_interview(self, user_reply: str) -> dict:
        """Phase 1 continued: Process user's reply to interview question.

        Returns same format as start_intake.
        """
        self.interview_turn += 1
        self.interview_history.append({"role": "user", "content": user_reply})

        if self._db_consultation:
            db.save_message(
                user_id=self.user_id,
                role="user",
                content=user_reply,
                consultation_id=self._db_consultation["id"],
            )

        # Check if max turns reached - if so, proceed with what we have
        if self.interview_turn >= self.MAX_INTERVIEW_TURNS:
            self._notify("interview", "done")
            return self._run_analysis_phase()

        question = self._interview_step(user_reply)

        if question:
            self.interview_history.append({"role": "assistant", "content": question})
            if self._db_consultation:
                db.save_message(
                    user_id=self.user_id,
                    role="assistant",
                    content=question,
                    consultation_id=self._db_consultation["id"],
                )
            return {
                "id": self.consultation["id"],
                "emergency": False,
                "needs_interview": True,
                "question": question,
                "consultation_id": self.consultation["id"],
            }
        else:
            self._notify("interview", "done")
            return self._run_analysis_phase()

    def _interview_step(self, latest_message: str) -> str | None:
        """Ask Claude consultant to either ask a follow-up or signal INTAKE_COMPLETE.

        Returns: follow-up question string, or None if intake is complete.
        """
        # Build conversation context
        convo_text = ""
        for msg in self.interview_history:
            role_label = "Benh nhan" if msg["role"] == "user" else "Jiva Health"
            convo_text += f"{role_label}: {msg['content']}\n\n"

        # Patient context from DB
        patient_context = ""
        if self.patient_id:
            patient = db.get_patient(self.patient_id)
            if patient:
                patient_context = (
                    f"\nThong tin benh nhan da biet:\n"
                    f"- Ten: {patient.get('name', 'N/A')}\n"
                    f"- Tuoi: {patient.get('age', 'N/A')}\n"
                    f"- Gioi: {patient.get('gender', 'N/A')}\n"
                    f"- Tien su: {patient.get('medical_history', 'N/A')}\n"
                    f"- Di ung: {patient.get('allergies', 'N/A')}\n"
                    f"- Thuoc hien tai: {patient.get('current_medications', 'N/A')}\n"
                )

        response = _call_claude(
            system_prompt=(
                f"{consultant.SYSTEM_PROMPT}\n\n"
                "## Luu y QUAN TRONG\n"
                "- Xung ho Toi/Ban (KHONG dung con/thay)\n"
                "- Neu DA DU thong tin (trieu chung, thoi gian, muc do, tien su co ban): "
                "bat dau response voi [INTAKE_COMPLETE] roi xuat YAML profile.\n"
                "- Neu CHUA DU: hoi them 1-2 cau ngan gon, than thien. "
                "KHONG hoi kieu checklist. Hoi TU NHIEN nhu bac si.\n"
                f"- Day la luot hoi thu {self.interview_turn + 1}/{self.MAX_INTERVIEW_TURNS}. "
                f"{'Neu la luot cuoi, hay ket thuc voi [INTAKE_COMPLETE].' if self.interview_turn + 1 >= self.MAX_INTERVIEW_TURNS else ''}"
            ),
            user_prompt=(
                f"{patient_context}\n"
                f"Cuoc hoi thoai:\n{convo_text}\n"
                "Tiep tuc hoi thoai hoac [INTAKE_COMPLETE] neu du thong tin."
            ),
        )

        if "[INTAKE_COMPLETE]" in response:
            # Extract profile and store
            self.consultation["patient_profile"] = {
                "raw_message": self.interview_history[0]["content"],
                "extracted": response,
                "full_conversation": convo_text,
            }
            if self._db_consultation:
                db.update_consultation(
                    self.consultation["id"],
                    patient_profile=response,
                )
            return None
        else:
            return response

    def _check_need_more_info(self, result: str) -> tuple[bool, str, str]:
        """Check if agent output contains [NEED_MORE_INFO] tag.

        Returns: (needs_info, question, clean_result)
        """
        if NEED_MORE_INFO_TAG in result:
            # Extract the question after the tag
            parts = result.split(NEED_MORE_INFO_TAG, 1)
            question = parts[1].strip()
            return True, question, parts[0].strip()
        return False, "", result

    def _run_analysis_phase(self) -> dict:
        """Phase 2: Run Steps 2-7 (research through handoff) after intake is complete.

        If any agent needs more info from the patient, returns with
        needs_clarification=True so the bot can ask the user and resume later.
        """
        consultation_id = self.consultation["id"]
        steps = {"emergency_check": "passed", "intake": "done"}

        # If profile not yet extracted (direct proceed), do quick intake
        if not self.consultation.get("patient_profile"):
            self._notify("intake", "start")
            all_messages = " ".join(
                msg["content"] for msg in self.interview_history if msg["role"] == "user"
            )
            profile = self._quick_intake(all_messages)
            self.consultation["patient_profile"] = profile
            if self._db_consultation:
                db.update_consultation(consultation_id, patient_profile=profile.get("extracted", ""))
            self._notify("intake", "done")

        profile = self.consultation["patient_profile"]

        # Resume from where we left off (if continuing after clarification)
        start_step = self._analysis_resume_step or "research"

        analysis_steps = [
            ("research", self._step_research, profile),
            ("eval", self._step_eval, profile),
            ("causes", self._step_causes, profile),
            ("solutions", self._step_solutions, profile),
            ("synthesis", self._step_synthesis, None),
            ("handoff", self._step_handoff, None),
        ]

        started = False
        for step_name, step_fn, step_arg in analysis_steps:
            if not started:
                if step_name == start_step:
                    started = True
                else:
                    # Already completed step
                    steps[step_name] = "done"
                    continue

            self._notify(step_name, "start")
            result_text = step_fn(step_arg) if step_arg is not None else step_fn()

            # Check if agent needs more info from patient
            needs_info, question, clean_result = self._check_need_more_info(result_text)
            if needs_info:
                # Save progress so we can resume after user replies
                self._analysis_resume_step = step_name
                self._analysis_pending_question_step = step_name
                if self._db_consultation:
                    db.save_message(
                        user_id=self.user_id,
                        role="assistant",
                        content=question,
                        consultation_id=self._db_consultation["id"],
                    )
                return {
                    "id": consultation_id,
                    "emergency": False,
                    "needs_interview": False,
                    "needs_clarification": True,
                    "clarification_question": question,
                    "clarification_step": step_name,
                    "consultation_id": consultation_id,
                }

            # Store result
            self._store_step_result(step_name, result_text)
            steps[step_name] = "done"
            if self._db_consultation:
                self._db_save_step(consultation_id, step_name, result_text)
            self._notify(step_name, "done")

        # Save report to file
        report_path = self._save_report(consultation_id, self.consultation["report"])

        # Update DB with final results
        if self._db_consultation:
            db.update_consultation(
                consultation_id,
                report=self.consultation["report"],
                handoff=self.consultation["handoff"],
                status="completed",
                completed_at=datetime.now().isoformat(),
            )
            db.save_message(
                user_id=self.user_id,
                role="assistant",
                content=self.consultation["handoff"],
                consultation_id=self._db_consultation["id"],
            )

        return {
            "id": consultation_id,
            "emergency": False,
            "needs_interview": False,
            "needs_clarification": False,
            "steps_completed": list(steps.keys()),
            "report": self.consultation["report"],
            "handoff": self.consultation["handoff"],
            "report_path": str(report_path),
        }

    def continue_after_clarification(self, user_reply: str) -> dict:
        """Resume analysis after user answered a clarification question.

        Adds the user's reply to the interview history (so subsequent agents
        can see it) and re-runs the step that asked for clarification.
        """
        self.interview_history.append({"role": "user", "content": user_reply})
        if self._db_consultation:
            db.save_message(
                user_id=self.user_id,
                role="user",
                content=user_reply,
                consultation_id=self._db_consultation["id"],
            )

        # Update profile with additional info
        profile = self.consultation.get("patient_profile", {})
        if isinstance(profile, dict):
            extra = profile.get("extra_info", [])
            extra.append(user_reply)
            profile["extra_info"] = extra
            self.consultation["patient_profile"] = profile

        # Resume analysis from the step that asked
        return self._run_analysis_phase()

    def _step_research(self, profile):
        return self._run_research(profile)

    def _step_eval(self, profile):
        return self._run_eval(
            profile,
            self.consultation.get("research_findings", ""),
        )

    def _step_causes(self, profile):
        return self._run_causes(
            profile,
            self.consultation.get("research_findings", ""),
        )

    def _step_solutions(self, profile):
        return self._run_solutions(
            profile,
            self.consultation.get("research_findings", ""),
            self.consultation.get("status_assessment", ""),
            self.consultation.get("causal_analysis", ""),
        )

    def _step_synthesis(self):
        return self._synthesize_report()

    def _step_handoff(self):
        return self._generate_handoff()

    def _store_step_result(self, step_name: str, result: str):
        """Store step result in consultation dict."""
        mapping = {
            "research": "research_findings",
            "eval": "status_assessment",
            "causes": "causal_analysis",
            "solutions": "solutions",
            "synthesis": "report",
            "handoff": "handoff",
        }
        key = mapping.get(step_name)
        if key:
            self.consultation[key] = result

    def _db_save_step(self, consultation_id: str, step_name: str, result: str):
        """Save step result to database."""
        mapping = {
            "research": "research_findings",
            "eval": "status_assessment",
            "causes": "causal_analysis",
            "solutions": "solutions",
        }
        field = mapping.get(step_name)
        if field:
            db.update_consultation(consultation_id, **{field: result})

    def start_consultation(self, patient_message: str) -> dict:
        """Legacy: Run full 8-step consultation without interactive interview.
        Kept for backward compatibility (CLI usage, etc.)."""
        consultation_id = f"c_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.consultation["id"] = consultation_id
        self.consultation["started_at"] = datetime.now().isoformat()

        if self.user_id:
            self._db_consultation = db.create_consultation(
                consultation_id=consultation_id,
                user_id=self.user_id,
                patient_id=self.patient_id,
                raw_message=patient_message,
            )
            db.save_message(
                user_id=self.user_id, role="user", content=patient_message,
                consultation_id=self._db_consultation["id"],
            )
        else:
            self._db_consultation = None

        # Emergency check
        self._notify("emergency_check", "start")
        emergency = self._check_emergency(patient_message)
        if emergency["is_emergency"]:
            if self._db_consultation:
                db.update_consultation(
                    consultation_id, is_emergency=1, status="emergency",
                    completed_at=datetime.now().isoformat(),
                )
            return {"id": consultation_id, "emergency": True, "message": emergency["message"],
                    "steps_completed": ["emergency_check"]}
        self._notify("emergency_check", "done")

        # Quick intake (no interview)
        self._notify("intake", "start")
        profile = self._quick_intake(patient_message)
        self.consultation["patient_profile"] = profile
        if self._db_consultation:
            db.update_consultation(consultation_id, patient_profile=profile.get("extracted", ""))
        self._notify("intake", "done")

        self.interview_history = [{"role": "user", "content": patient_message}]
        return self._run_analysis_phase()

    def _check_emergency(self, message: str) -> dict:
        """Step 0: Emergency keyword check."""
        message_lower = message.lower()
        found = [kw for kw in EMERGENCY_KEYWORDS if kw in message_lower]

        if found:
            return {
                "is_emergency": True,
                "keywords": found,
                "message": (
                    "CẢNH BÁO KHẨN CẤP\n\n"
                    f"Phát hiện triệu chứng khẩn cấp: {', '.join(found)}\n\n"
                    "GỌI NGAY:\n"
                    "VN: 115 (Cấp cứu)\n"
                    "US: 911 (Emergency)\n\n"
                    "Đây có thể là tình huống cần cấp cứu. "
                    "Vui lòng đến cơ sở y tế gần nhất NGAY."
                ),
            }
        return {"is_emergency": False}

    def _quick_intake(self, message: str) -> dict:
        """Step 1: Extract patient profile from message."""
        # Include patient history if available
        patient_context = ""
        if self.patient_id:
            patient = db.get_patient(self.patient_id)
            if patient:
                patient_context = (
                    f"\nThong tin benh nhan da biet:\n"
                    f"- Ten: {patient.get('name', 'N/A')}\n"
                    f"- Tuoi: {patient.get('age', 'N/A')}\n"
                    f"- Gioi: {patient.get('gender', 'N/A')}\n"
                    f"- Tien su: {patient.get('medical_history', 'N/A')}\n"
                    f"- Di ung: {patient.get('allergies', 'N/A')}\n"
                    f"- Thuoc hien tai: {patient.get('current_medications', 'N/A')}\n"
                )

        # Include recent consultation history
        history_context = ""
        if self.patient_id:
            past = db.get_consultations_by_patient(self.patient_id, limit=3)
            if past:
                history_context = "\nLich su kham gan day:\n"
                for c in past:
                    if c.get("chief_complaint"):
                        history_context += f"- {c['created_at'][:10]}: {c['chief_complaint'][:100]}\n"

        response = _call_claude(
            system_prompt=consultant.SYSTEM_PROMPT,
            user_prompt=(
                f"Benh nhan gui tin nhan sau. "
                f"Trich xuat Patient Profile tu thong tin co san. "
                f"Nhung gi chua biet thi ghi 'chua ro'.\n\n"
                f"{patient_context}"
                f"{history_context}"
                f"\nTin nhan: {message}\n\n"
                f"Output YAML patient_profile."
            ),
        )
        return {"raw_message": message, "extracted": response}

    def _run_research(self, profile: dict) -> str:
        """Step 2: Research medical literature."""
        messages = research.build_research_prompt(profile)
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]

        # Include knowledge base entries as additional context
        kb_entries = db.get_knowledge(limit=20)
        if kb_entries:
            kb_text = "\n".join(f"- {e['content']}" for e in kb_entries)
            user_msg += f"\n\nKnowledge Base (thong tin bo sung tu chuyen gia):\n{kb_text}"

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

def run_consultation(message: str, user_id: int = None, patient_id: int = None) -> dict:
    """Quick function to run a full consultation."""
    orchestrator = HealthOrchestrator(user_id=user_id, patient_id=patient_id)
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
