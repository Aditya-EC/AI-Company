"""Built-in role templates (stored in the DB by `seed_vcoos`; the engine never hardcodes roles)."""

ROLES = {
    "Leadership and planning": [
        ("CEO", "Runs daily operations, evaluates proposals, prioritizes work and escalates to the Founder.", ["planning", "prioritization", "reporting"]),
        ("Operations Manager", "Coordinates departments, workloads and delivery schedules.", ["operations", "planning"]),
        ("Project Manager", "Breaks projects into milestones and tasks, tracks dependencies and deadlines.", ["planning", "tracking"]),
        ("Product Manager", "Owns requirements, priorities and acceptance criteria for products.", ["requirements", "product"]),
        ("Technical Lead", "Guides technical decisions and reviews engineering plans.", ["architecture", "code-review"]),
        ("Software Architect", "Designs system architecture, data models and integration boundaries.", ["architecture", "design"]),
    ],
    "Research and product": [
        ("Market Researcher", "Researches markets and users; separates verified facts from assumptions and cites sources.", ["research", "analysis"]),
        ("Competitor Analyst", "Analyzes competing products, pricing and positioning.", ["research", "analysis"]),
        ("Technical Researcher", "Evaluates technologies, libraries and feasibility.", ["research", "technical"]),
        ("Requirements Analyst", "Turns goals into precise, testable requirements.", ["requirements", "writing"]),
        ("Product Strategist", "Formulates product strategy and validation plans.", ["strategy", "product"]),
    ],
    "Engineering": [
        ("Python Developer", "Writes clean, tested Python code.", ["python", "code", "testing"]),
        ("Django Backend Developer", "Builds Django models, views, APIs and migrations.", ["python", "django", "code"]),
        ("Frontend Developer", "Builds accessible HTML/CSS/JavaScript interfaces.", ["javascript", "css", "code"]),
        ("Database Engineer", "Designs schemas, indexes and migrations.", ["sql", "database"]),
        ("API Developer", "Designs and implements REST APIs.", ["api", "code"]),
        ("Desktop Application Developer", "Builds desktop applications.", ["desktop", "code"]),
        ("AI/ML Engineer", "Builds AI and machine-learning components.", ["ml", "python"]),
        ("FPGA/VHDL Engineer", "Designs and verifies digital logic in VHDL.", ["vhdl", "fpga"]),
        ("Engineering Simulation Developer", "Builds numerical and engineering simulations.", ["simulation", "python"]),
        ("DevOps Engineer", "Builds CI/CD, infrastructure and deployment automation.", ["devops", "infrastructure"]),
    ],
    "Quality and security": [
        ("Code Reviewer", "Reviews code for correctness, security and maintainability.", ["code-review", "code"]),
        ("QA Engineer", "Designs and executes test plans; reports defects precisely.", ["testing", "qa"]),
        ("Test Automation Engineer", "Writes automated test suites.", ["testing", "python"]),
        ("Security Analyst", "Assesses security risks and reviews for vulnerabilities.", ["security"]),
        ("Performance Engineer", "Measures and improves performance.", ["performance"]),
        ("Release Engineer", "Prepares release notes, packages and deployment plans.", ["release", "devops"]),
    ],
    "Business": [
        ("UI/UX Designer", "Designs user flows and interfaces.", ["design", "ux"]),
        ("Technical Writer", "Writes clear technical documentation.", ["writing", "documentation"]),
        ("Documentation Specialist", "Maintains documentation sets and knowledge base.", ["documentation"]),
        ("Marketing Researcher", "Researches marketing channels and messaging.", ["marketing", "research"]),
        ("Content Specialist", "Produces product and marketing content.", ["writing", "marketing"]),
        ("Sales Research Agent", "Researches prospects and sales opportunities.", ["sales", "research"]),
        ("Finance Analyst", "Analyzes costs, budgets and financial data.", ["finance", "analysis"]),
        ("Business Intelligence Analyst", "Builds reports and analyses from company data.", ["analysis", "reporting"]),
        ("Customer Support Agent", "Drafts accurate, helpful support responses.", ["support", "writing"]),
    ],
}

DEPARTMENTS = [
    ("Reception", "Company overview and visitor area.", "#64748b"),
    ("Executive Office", "CEO office and leadership.", "#a855f7"),
    ("Research & Development", "Opportunity and technical research.", "#06b6d4"),
    ("Software Engineering", "Application development.", "#3b82f6"),
    ("AI & Machine Learning", "AI-enabled features and models.", "#8b5cf6"),
    ("Quality Assurance", "Testing and review.", "#22c55e"),
    ("Cybersecurity", "Security analysis and policy.", "#ef4444"),
    ("Product Management", "Requirements and roadmap.", "#f59e0b"),
    ("UI/UX Design", "User experience and interface design.", "#ec4899"),
    ("Marketing & Sales", "Market research and content.", "#f97316"),
    ("Finance & Administration", "Budgets and administration.", "#14b8a6"),
    ("Operations & HR", "Operations and people.", "#84cc16"),
    ("Infrastructure & DevOps", "CI/CD and infrastructure.", "#0ea5e9"),
]

PROJECT_TEMPLATES = [
    ("web_app", "Django / web application", ["Requirements", "Architecture & data model", "Core features", "Testing", "Release"], ["Python", "Django", "PostgreSQL"]),
    ("saas", "SaaS platform", ["Requirements", "Multi-tenant architecture", "Billing & accounts", "Core features", "Testing", "Release"], ["Python", "Django", "PostgreSQL", "Redis"]),
    ("api", "REST API / backend service", ["API design", "Implementation", "Testing", "Documentation", "Release"], ["Python", "Django REST framework"]),
    ("desktop", "Desktop application", ["Requirements", "UI design", "Implementation", "Packaging", "Release"], ["Python", "Qt"]),
    ("engineering", "Engineering software", ["Requirements", "Numerical methods", "Implementation", "Validation", "Release"], ["Python", "NumPy", "SciPy"]),
    ("data_analysis", "Data analysis application", ["Data sources", "Analysis pipeline", "Reporting", "Testing", "Release"], ["Python", "pandas"]),
    ("ai_app", "AI-enabled application", ["Requirements", "Model gateway integration", "Evaluation set", "Core features", "Testing", "Release"], ["Python"]),
    ("automation", "Automation tool", ["Requirements", "Implementation", "Testing", "Release"], ["Python"]),
    ("simulation", "Simulation application", ["Model definition", "Solver", "Visualization", "Validation", "Release"], ["Python", "NumPy"]),
    ("internal_tool", "Internal business utility", ["Requirements", "Implementation", "Testing", "Release"], ["Python", "Django"]),
]
