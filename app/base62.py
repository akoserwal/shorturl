ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)


def encode(num: int) -> str:
    if num == 0:
        return ALPHABET[0]
    chars = []
    while num > 0:
        chars.append(ALPHABET[num % BASE])
        num //= BASE
    return "".join(reversed(chars))


def decode(s: str) -> int:
    num = 0
    for char in s:
        num = num * BASE + ALPHABET.index(char)
    return num
