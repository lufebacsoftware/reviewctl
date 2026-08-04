ACCOUNT_REQUIREMENTS = {"receivable": {"party"}}


def post_line(account_code, dimensions, amount):
    return {"account": account_code, "dimensions": dimensions, "amount": amount}
