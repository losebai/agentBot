"""分割发布包中的普通 SQL 语句，保留字符串中的分号和注释标记。"""


def split_sql(script: str) -> list[str]:
    """仅处理已登记的不含存储过程/触发器的增量，不扫描任意 SQL 目录。"""
    statements, current = [], []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        following = script[index : index + 2]
        if quote:
            current.append(char)
            if char == "\\" and index + 1 < len(script):
                index += 1
                current.append(script[index])
            elif char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    index += 1
                    current.append(script[index])
                else:
                    quote = None
        elif following == "--":
            end = script.find("\n", index)
            index = len(script) if end < 0 else end
            current.append(" ")
        elif following == "/*":
            end = script.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated SQL comment")
            index = end + 1
            current.append(" ")
        elif char in "'\"`":
            quote = char
            current.append(char)
        elif char == ";":
            if "".join(current).strip():
                statements.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    if quote:
        raise ValueError("unterminated SQL string")
    if "".join(current).strip():
        statements.append("".join(current).strip())
    return statements
