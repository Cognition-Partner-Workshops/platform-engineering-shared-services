"""Approval workflow for destructive actions."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig

logger = logging.getLogger("oracle_db_agent.approval")


class PendingApproval:
    """Represents a single pending approval request."""

    def __init__(
        self,
        action: str,
        description: str,
        sql_or_command: str,
        approval_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        self.approval_id = approval_id or str(uuid.uuid4())[:8]
        self.action = action
        self.description = description
        self.sql_or_command = sql_or_command
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.status = "pending"

    def to_dict(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "action": self.action,
            "description": self.description,
            "sql_or_command": self.sql_or_command,
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingApproval:
        obj = cls(
            action=data["action"],
            description=data["description"],
            sql_or_command=data["sql_or_command"],
            approval_id=data.get("approval_id"),
            created_at=data.get("created_at"),
        )
        obj.status = data.get("status", "pending")
        return obj


class ApprovalManager:
    """Manages approval workflow for destructive database actions.

    When a destructive action is requested:
    1. The action is logged to a pending approvals JSON file
    2. An email notification is sent to the DBA team
    3. The DBA approves/rejects by running: db-agent approve <id> / db-agent reject <id>
    4. Only approved actions are executed
    """

    def __init__(self, config: AgentConfig, alerter: EmailAlerter) -> None:
        self._config = config
        self._alerter = alerter
        self._approval_file = Path(config.approval.approval_file)
        self._actions_requiring_approval = set(config.approval.actions_requiring_approval)

    def requires_approval(self, action: str) -> bool:
        """Check if an action requires approval."""
        if not self._config.approval.require_approval:
            return False
        return action in self._actions_requiring_approval

    def request_approval(self, action: str, description: str, sql_or_command: str) -> PendingApproval:
        """Create a pending approval request and notify via email."""
        pending = PendingApproval(
            action=action,
            description=description,
            sql_or_command=sql_or_command,
        )
        self._save_pending(pending)

        # Send email notification
        self._alerter.send_alert(
            subject=f"Approval Required: {action} [{pending.approval_id}]",
            body_html=self._format_approval_email(pending),
        )

        logger.info(
            "Approval requested: id=%s action=%s desc=%s",
            pending.approval_id,
            action,
            description,
        )
        return pending

    def approve(self, approval_id: str) -> bool:
        """Approve a pending action by its ID."""
        return self._update_status(approval_id, "approved")

    def reject(self, approval_id: str) -> bool:
        """Reject a pending action by its ID."""
        return self._update_status(approval_id, "rejected")

    def get_pending(self) -> list[PendingApproval]:
        """Get all pending approval requests."""
        all_approvals = self._load_all()
        return [a for a in all_approvals if a.status == "pending"]

    def get_approved(self) -> list[PendingApproval]:
        """Get all approved (ready to execute) requests."""
        all_approvals = self._load_all()
        return [a for a in all_approvals if a.status == "approved"]

    def mark_executed(self, approval_id: str) -> bool:
        """Mark an approved action as executed."""
        return self._update_status(approval_id, "executed")

    def _save_pending(self, pending: PendingApproval) -> None:
        """Append a pending approval to the file."""
        approvals = self._load_all()
        approvals.append(pending)
        self._write_all(approvals)

    def _update_status(self, approval_id: str, new_status: str) -> bool:
        """Update the status of a specific approval."""
        approvals = self._load_all()
        for approval in approvals:
            if approval.approval_id == approval_id:
                approval.status = new_status
                self._write_all(approvals)
                logger.info("Approval %s status changed to: %s", approval_id, new_status)
                return True
        logger.warning("Approval ID not found: %s", approval_id)
        return False

    def _load_all(self) -> list[PendingApproval]:
        """Load all approvals from the JSON file."""
        if not self._approval_file.exists():
            return []
        try:
            with open(self._approval_file, encoding="utf-8") as f:
                data = json.load(f)
            return [PendingApproval.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Error reading approvals file: %s", exc)
            return []

    def _write_all(self, approvals: list[PendingApproval]) -> None:
        """Write all approvals to the JSON file."""
        os.makedirs(self._approval_file.parent, exist_ok=True)
        with open(self._approval_file, "w", encoding="utf-8") as f:
            json.dump([a.to_dict() for a in approvals], f, indent=2)

    def _format_approval_email(self, pending: PendingApproval) -> str:
        """Format the approval request as an HTML email."""
        return f"""
        <html><body>
        <h3>Approval Required</h3>
        <table border="1" cellpadding="8" cellspacing="0">
            <tr><td><strong>Approval ID</strong></td><td><code>{pending.approval_id}</code></td></tr>
            <tr><td><strong>Action</strong></td><td>{pending.action}</td></tr>
            <tr><td><strong>Description</strong></td><td>{pending.description}</td></tr>
            <tr><td><strong>SQL / Command</strong></td><td><pre>{pending.sql_or_command}</pre></td></tr>
            <tr><td><strong>Requested At</strong></td><td>{pending.created_at}</td></tr>
        </table>
        <br/>
        <p>To approve, run: <code>db-agent approve {pending.approval_id}</code></p>
        <p>To reject, run: <code>db-agent reject {pending.approval_id}</code></p>
        </body></html>
        """
