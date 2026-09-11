"""
review_emails.py

Step 4 of the pipeline (manual review, spec section 4): print each drafted
email one at a time and let a human approve, edit, or skip it before it's
eligible for sending. Nothing is ever sent automatically -- send_emails.py
only sends leads whose status is `ready_to_send`, which only this script
(or a human editing leads.xlsx directly) can set.

Usage:
    python review_emails.py
"""

from common import (
    STATUS_DRAFTED,
    STATUS_READY_TO_SEND,
    STATUS_SKIPPED,
    read_leads,
    write_leads,
)


def prompt_choice():
    while True:
        choice = input("[a]pprove / [e]dit / [s]kip / [q]uit review? ").strip().lower()
        if choice in ("a", "e", "s", "q"):
            return choice
        print("Please enter a, e, s, or q.")


def edit_draft(current_text):
    print("\nEnter the revised email body. Finish with a single line containing just: END")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip() or current_text


def run():
    leads = read_leads()
    to_review = [row for row in leads if row["status"] == STATUS_DRAFTED]

    if not to_review:
        print("No drafted emails awaiting review. Run generate_emails.py first.")
        return

    print(f"{len(to_review)} draft(s) to review.\n")

    reviewed = 0
    for lead in to_review:
        print("=" * 70)
        print(f"To:      {lead['name']} <{lead['email']}>")
        print(f"Address: {lead['address']}")
        print(f"Phone:   {lead['phone']}")
        print("-" * 70)
        print(lead["draft_email"])
        print("=" * 70)

        choice = prompt_choice()
        if choice == "q":
            print("Stopping review. Progress so far has been saved.")
            break
        elif choice == "a":
            lead["status"] = STATUS_READY_TO_SEND
            print("-> approved, marked ready_to_send.\n")
        elif choice == "e":
            lead["draft_email"] = edit_draft(lead["draft_email"])
            lead["status"] = STATUS_READY_TO_SEND
            print("-> edited and marked ready_to_send.\n")
        elif choice == "s":
            lead["status"] = STATUS_SKIPPED
            print("-> skipped.\n")
        reviewed += 1

    write_leads(leads)
    print(f"Done. Reviewed {reviewed} draft(s). Run send_emails.py next to send approved emails.")


if __name__ == "__main__":
    run()
