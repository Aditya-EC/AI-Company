from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.core import audit
from apps.core.models import CompanySettings, Department, RoleTemplate
from apps.core.roles import DEPARTMENTS, ROLES
from apps.tools.registry import sync_definitions


class Command(BaseCommand):
    help = "Install built-in role templates and tool definitions. Optionally create the standard department layout."

    def add_arguments(self, parser):
        parser.add_argument("--with-departments", action="store_true", help="Create the 13 standard departments/rooms.")

    def handle(self, *args, with_departments=False, **kw):
        CompanySettings.get()
        n = 0
        for category, roles in ROLES.items():
            for name, desc, skills in roles:
                _, created = RoleTemplate.objects.update_or_create(
                    name=name, defaults=dict(category=category, job_title=name, description=desc, skills=skills, is_builtin=True,
                                             system_instructions=f"You are a {name}. {desc}", required_capabilities=["json"]))
                n += created
        sync_definitions()
        self.stdout.write(f"Role templates installed ({n} new). Tool definitions synced.")
        if with_departments:
            for i, (name, desc, color) in enumerate(DEPARTMENTS):
                Department.objects.get_or_create(slug=slugify(name), defaults=dict(
                    name=name, description=desc, color=color, office_x=i % 4, office_z=i // 4))
            self.stdout.write(f"{len(DEPARTMENTS)} departments ensured.")
        audit.record("system.seeded", details={"departments": with_departments})
