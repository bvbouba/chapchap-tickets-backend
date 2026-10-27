from django.contrib.auth.hashers import PBKDF2PasswordHasher


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    """Low-iteration PBKDF2 for the isolated PrintMe tests.

    The hash still does not contain the plaintext. Production does not use
    this hasher.
    """

    iterations = 1
