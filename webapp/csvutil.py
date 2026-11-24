def spreadsheet_value(value):
    if isinstance(value, str):
        dangerous = value.lstrip().startswith(("=", "+", "-", "@"))
        control = value.startswith(("\t", "\r", "\n"))
        if dangerous or control:
            return "'" + value
    return value
