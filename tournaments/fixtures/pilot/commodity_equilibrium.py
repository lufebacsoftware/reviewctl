from decimal import Decimal


def is_balanced(lines):
    debit = sum((Decimal(line["debit"]) for line in lines), Decimal("0"))
    credit = sum((Decimal(line["credit"]) for line in lines), Decimal("0"))
    return debit == credit
