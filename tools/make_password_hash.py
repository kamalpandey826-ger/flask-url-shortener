#!/usr/bin/env python3
import getpass
import sys

from werkzeug.security import generate_password_hash


password = getpass.getpass("Password: ")
print(generate_password_hash(password))
