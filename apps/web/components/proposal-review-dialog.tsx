"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type ProposalReviewAction = "approve" | "reject" | "apply";

export type ProposalReviewSubmission = {
  action: ProposalReviewAction;
  reason: string;
};

export type ProposalReviewDialogProps = {
  open: boolean;
  onClose: () => void;
  onSubmit: (submission: ProposalReviewSubmission) => void | Promise<void>;
  loading?: boolean;
  title?: string;
  description?: ReactNode;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  initialReason?: string;
  actions?: ProposalReviewAction[];
};

const focusableSelector = [
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "a[href]",
  "[tabindex]:not([tabindex=\"-1\"]):not([aria-disabled=\"true\"])",
].join(",");

export function ProposalReviewDialog({
  open,
  onClose,
  onSubmit,
  loading = false,
  title = "审核知识更新提案",
  description = "请选择审核动作，并补充本次决定的原因。",
  reasonLabel = "审核原因",
  reasonPlaceholder = "请输入原因（可选）",
  initialReason = "",
  actions = ["reject", "approve", "apply"],
}: ProposalReviewDialogProps) {
  const [reason, setReason] = useState(initialReason);
  const [isApplyConfirmation, setIsApplyConfirmation] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;

    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    reasonRef.current?.focus();
    return () => {
      const opener = openerRef.current;
      openerRef.current = null;
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    document.body.classList.add("proposal-review-dialog-open");
    return () => document.body.classList.remove("proposal-review-dialog-open");
  }, [open]);

  if (!open || typeof document === "undefined") return null;

  const close = () => {
    if (loading) return;
    setIsApplyConfirmation(false);
    onClose();
  };

  const submit = (action: ProposalReviewAction) => {
    if (loading) return;
    if (action === "apply" && !isApplyConfirmation) {
      setIsApplyConfirmation(true);
      return;
    }
    onSubmit({ action, reason });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }

    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(focusableSelector);
    if (!focusable || focusable.length === 0) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return createPortal(
    <div
      className="proposal-review-dialog-backdrop"
      data-testid="proposal-review-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget || loading) return;
        event.preventDefault();
        close();
      }}
    >
      <div
        aria-describedby="proposal-review-dialog-description"
        aria-labelledby="proposal-review-dialog-title"
        aria-modal="true"
        className="proposal-review-dialog"
        onKeyDown={handleKeyDown}
        ref={dialogRef}
        role="dialog"
      >
        <div className="proposal-review-dialog-heading">
          <div>
            <p className="state-kicker">PROPOSAL REVIEW</p>
            <h2 id="proposal-review-dialog-title">{title}</h2>
            <p id="proposal-review-dialog-description">{description}</p>
          </div>
          <button
            aria-label="关闭审核弹窗"
            className="proposal-review-dialog-close"
            disabled={loading}
            onClick={close}
            type="button"
          >
            ×
          </button>
        </div>

        <label className="proposal-review-dialog-field">
          <span>{reasonLabel}</span>
          <textarea
            disabled={loading}
            onChange={(event) => setReason(event.target.value)}
            placeholder={reasonPlaceholder}
            ref={reasonRef}
            value={reason}
          />
        </label>

        {isApplyConfirmation && (
          <div aria-live="assertive" className="proposal-review-dialog-confirmation" role="alert">
            <strong>确认应用此提案？</strong>
            <span>应用后将直接写入知识库，请再次确认。</span>
          </div>
        )}

        <div className="proposal-review-dialog-actions">
          {actions.includes("reject") && (
            <button className="button" disabled={loading} onClick={() => submit("reject")} type="button">
              {loading ? "提交中…" : "拒绝"}
            </button>
          )}
          {actions.includes("approve") && (
            <button className="button" disabled={loading} onClick={() => submit("approve")} type="button">
              {loading ? "提交中…" : "批准"}
            </button>
          )}
          {actions.includes("apply") && (
            <button className="button primary" disabled={loading} onClick={() => submit("apply")} type="button">
              {loading ? "提交中…" : isApplyConfirmation ? "确认应用" : "应用"}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
