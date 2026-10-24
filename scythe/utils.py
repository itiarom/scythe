def read_file(file_path: str) -> str:
    with open(file_path, 'r') as file:
        return file.read()


def update_file(file_path: str, content: str):
    with open(file_path, 'w') as file:
        file.write(content)


def remove_empty_lines(source_code):
    lines = source_code.split("\n")
    non_empty_lines = [line for line in lines if line.strip() != ""]
    return "\n".join(non_empty_lines)
