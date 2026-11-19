import hashlib


def calculate_sha256(file_path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as file_object:
        for chunk in iter(lambda: file_object.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def verify_file_checksum(file_path, expected_sha256: str) -> bool:
    if not expected_sha256:
        return False
    return calculate_sha256(file_path) == expected_sha256
