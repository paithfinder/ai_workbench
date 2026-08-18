import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ProposalReviewDialog, type ProposalReviewDialogProps } from "./proposal-review-dialog";

function renderDialog(overrides: Partial<ProposalReviewDialogProps> = {}) {
  const onClose = vi.fn();
  const onSubmit = vi.fn();
  render(<button type="button">打开审核</button>);
  const opener = screen.getByRole("button", { name: "打开审核" });
  opener.focus();
  render(<ProposalReviewDialog onClose={onClose} onSubmit={onSubmit} open {...overrides} />);
  return { onClose, onSubmit, opener };
}

describe("ProposalReviewDialog", () => {
  it("focuses the reason field and submits approve/reject with the reason", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderDialog();
    const reason = await screen.findByRole("textbox", { name: "审核原因" });

    expect(reason).toHaveFocus();
    await user.type(reason, "内容已核验");
    await user.click(screen.getByRole("button", { name: "批准" }));
    expect(onSubmit).toHaveBeenCalledWith({ action: "approve", reason: "内容已核验" });

    await user.click(screen.getByRole("button", { name: "拒绝" }));
    expect(onSubmit).toHaveBeenCalledWith({ action: "reject", reason: "内容已核验" });
  });

  it("requires explicit confirmation before apply", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderDialog();

    await user.click(screen.getByRole("button", { name: "应用" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("确认应用此提案？");
    await user.click(screen.getByRole("button", { name: "确认应用" }));
    expect(onSubmit).toHaveBeenCalledWith({ action: "apply", reason: "" });
  });

  it("closes from Escape and the backdrop, then restores opener focus", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSubmit = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)} type="button">打开审核</button>
          <ProposalReviewDialog
            onClose={() => {
              onClose();
              setOpen(false);
            }}
            onSubmit={onSubmit}
            open={open}
          />
        </>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole("button", { name: "打开审核" });
    await user.click(opener);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(opener).toHaveFocus();

    await user.click(opener);
    await user.click(screen.getByTestId("proposal-review-dialog-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(opener).toHaveFocus();
  });

  it("traps Tab and Shift+Tab within the dialog", async () => {
    const user = userEvent.setup();
    renderDialog();
    const close = screen.getByRole("button", { name: "关闭审核弹窗" });
    const reason = screen.getByRole("textbox", { name: "审核原因" });
    const reject = screen.getByRole("button", { name: "拒绝" });
    const approve = screen.getByRole("button", { name: "批准" });
    const apply = screen.getByRole("button", { name: "应用" });

    expect(reason).toHaveFocus();
    await user.tab();
    expect(reject).toHaveFocus();
    await user.tab();
    expect(approve).toHaveFocus();
    await user.tab();
    expect(apply).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(apply).toHaveFocus();

    close.focus();
    await user.tab({ shift: true });
    expect(apply).toHaveFocus();
  });

  it("prevents every interaction and duplicate submit while loading", async () => {
    const user = userEvent.setup();
    const { onClose, onSubmit } = renderDialog({ loading: true });
    const reason = screen.getByRole("textbox", { name: "审核原因" });

    expect(reason).toBeDisabled();
    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    await user.click(screen.getByRole("button", { name: "关闭审核弹窗" }));
    await user.click(screen.getByTestId("proposal-review-dialog-backdrop"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not render when closed", () => {
    renderDialog({ open: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
