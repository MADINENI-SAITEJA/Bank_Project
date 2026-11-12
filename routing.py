"""Map BANKING77 issue categories to the five support queues."""


def queue_for(category):
    """Route the issue type first, avoiding substring matches such as pin in topping."""
    label = category.lower()
    tokens = set(label.split("_"))
    if "top_up" in label or "topping_up" in label:
        return "Top-ups"
    if tokens & {"cash", "atm", "withdrawal", "swallowed"}:
        return "Cash & ATM"
    if (
        tokens & {"transfer", "beneficiary"}
        or "direct_debit" in label
        or label == "receiving_money"
    ):
        return "Transfers"
    if tokens & {"card", "cards", "pin", "contactless", "visa"} or "apple_pay" in label:
        return "Cards"
    return "Account support"
