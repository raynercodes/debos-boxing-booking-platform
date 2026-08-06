"""
Computes the admin password hash against the REAL deployed pepper secret,
and prints it for you to push into Secrets Manager.

Run with AWS_PROFILE=debos-boxing (day-to-day, NOT the deploy role) — reading
the pepper and setting the final hash value isn't infrastructure creation,
so it deliberately doesn't need the elevated debos-boxing-deploy profile.

Usage:
    export AWS_PROFILE=debos-boxing
    export AWS_DEFAULT_REGION=us-east-1
    export JWT_SECRET_PATH=/debos-boxing/dev/jwt-secret
    export ADMIN_CREDENTIALS_PATH=/debos-boxing/dev/admin-credentials
    export PASSWORD_PEPPER_PATH=/debos-boxing/dev/password-pepper
    python3 scripts/set_admin_password.py

Kept as a permanent utility (not a one-time throwaway script) since you'll
want this again for password ROTATION later, not just initial setup.
"""

import getpass
from src.api.core.security import get_security_service

password = getpass.getpass("Enter the real admin password (hidden, won't echo): ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    print("Passwords don't match — aborting, nothing was changed.")
    exit(1)

if len(password) < 12:
    print("Warning: that's a short password for an admin account protecting client data.")
    proceed = input("Continue anyway? (y/N): ")
    if proceed.lower() != "y":
        exit(1)

hash_value = get_security_service().hash_password(password)

print("\nComputed hash — copy the FULL value below (nothing else, no extra quotes):")
print(hash_value)
print("\nThen run this command to push it into Secrets Manager:")
print(f'aws secretsmanager put-secret-value --secret-id /debos-boxing/dev/admin-credentials '
      f'--secret-string \'{{"password_hash":"{hash_value}"}}\' --profile debos-boxing --region us-east-1')