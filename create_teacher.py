"""
create_teacher.py — run this once from the terminal to create the first
teacher account. After that, teachers can be added the same way, or you can
build an "Add Teacher" admin page later — for a single-classroom pilot, this
script is enough.

Usage:
    python create_teacher.py
"""

import getpass
from auth import init_users_table, add_user

def main():
    init_users_table()
    print("Create a teacher account.")
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")

    if password != confirm:
        print("Passwords don't match. Try again.")
        return

    try:
        add_user(username=username, password=password, role="teacher")
        print(f"Teacher account '{username}' created.")
    except ValueError as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
