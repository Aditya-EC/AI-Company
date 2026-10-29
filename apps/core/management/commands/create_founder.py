import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError

from apps.core import audit
from apps.core.models import User


class Command(BaseCommand):
    help = "Create the Founder account (password from VCOOS_FOUNDER_PASSWORD or prompt)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--email", default="")

    def handle(self, username, email, **kw):
        if User.objects.filter(account_type=User.FOUNDER, is_active=True).exists():
            raise CommandError("An active Founder already exists.")
        password = os.environ.get("VCOOS_FOUNDER_PASSWORD") or getpass.getpass("Founder password: ")
        validate_password(password)
        user = User.objects.create_user(username=username, email=email, password=password, account_type=User.FOUNDER,
                                        is_staff=True, is_superuser=True)
        audit.record("account.founder_created", target=user)
        self.stdout.write(self.style.SUCCESS(f"Founder '{username}' created."))
