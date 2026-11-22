"""Canonical skill vocabulary with aliases, shared by resume parsing and job matching.

`find_skills(text)` returns canonical names found in text, alias-aware and with
boundaries that work for tricky names (C++, C#, .NET, Node.js). Ambiguous one-letter
or common-word skills ("Go", "R", "C", "Express") need an unambiguous alias.
"""

from __future__ import annotations

import re
from functools import cache

# canonical: (category, [aliases])  category ∈ languages|frameworks|tools|cloud|databases|ml_ai|soft
_VOCAB: dict[str, tuple[str, list[str]]] = {
    # languages
    "Python": ("languages", ["python3", "py"]),
    "Java": ("languages", []),
    "JavaScript": ("languages", ["js", "ecmascript", "es6"]),
    "TypeScript": ("languages", ["ts"]),
    "C++": ("languages", ["cpp"]),
    "C#": ("languages", ["csharp", "c sharp"]),
    "Go": ("languages", ["golang"]),
    "Rust": ("languages", []),
    "Kotlin": ("languages", []),
    "Swift": ("languages", []),
    "PHP": ("languages", []),
    "Ruby": ("languages", []),
    "Scala": ("languages", []),
    "Dart": ("languages", []),
    "SQL": ("languages", []),
    "Bash": ("languages", ["shell scripting", "shell script"]),
    "HTML": ("languages", ["html5"]),
    "CSS": ("languages", ["css3"]),
    "MATLAB": ("languages", []),
    "Solidity": ("languages", []),
    # frameworks / libraries
    "React": ("frameworks", ["react.js", "reactjs"]),
    "Next.js": ("frameworks", ["nextjs", "next js"]),
    "Vue.js": ("frameworks", ["vue", "vuejs"]),
    "Angular": ("frameworks", ["angularjs"]),
    "Svelte": ("frameworks", []),
    "Node.js": ("frameworks", ["node", "nodejs", "node js"]),
    "Express.js": ("frameworks", ["expressjs", "express.js"]),
    "NestJS": ("frameworks", ["nest.js"]),
    "Django": ("frameworks", []),
    "Flask": ("frameworks", []),
    "FastAPI": ("frameworks", []),
    "Spring Boot": ("frameworks", ["spring", "springboot"]),
    "ASP.NET": ("frameworks", [".net", "dotnet", ".net core"]),
    "Laravel": ("frameworks", []),
    "Ruby on Rails": ("frameworks", ["rails"]),
    "Flutter": ("frameworks", []),
    "React Native": ("frameworks", []),
    "Tailwind CSS": ("frameworks", ["tailwind", "tailwindcss"]),
    "Bootstrap": ("frameworks", []),
    "jQuery": ("frameworks", []),
    "GraphQL": ("frameworks", []),
    "REST APIs": ("frameworks", ["rest api", "restful", "rest", "restful apis"]),
    "gRPC": ("frameworks", []),
    "Pandas": ("frameworks", []),
    "NumPy": ("frameworks", []),
    "Celery": ("frameworks", []),
    "Selenium": ("frameworks", []),
    "Playwright": ("frameworks", []),
    "Pytest": ("frameworks", []),
    "Jest": ("frameworks", []),
    "PySpark": ("frameworks", ["spark", "apache spark"]),
    "Hadoop": ("frameworks", []),
    "Kafka": ("frameworks", ["apache kafka"]),
    "RabbitMQ": ("frameworks", []),
    "WordPress": ("frameworks", []),
    "Shopify": ("frameworks", []),
    # tools
    "Git": ("tools", []),
    "GitHub": ("tools", []),
    "GitLab": ("tools", []),
    "Docker": ("tools", []),
    "Kubernetes": ("tools", ["k8s"]),
    "Terraform": ("tools", []),
    "Ansible": ("tools", []),
    "Jenkins": ("tools", []),
    "GitHub Actions": ("tools", []),
    "CI/CD": ("tools", ["ci cd", "cicd"]),
    "Linux": ("tools", ["ubuntu"]),
    "Nginx": ("tools", []),
    "Postman": ("tools", []),
    "Jira": ("tools", []),
    "Figma": ("tools", []),
    "Tableau": ("tools", []),
    "Power BI": ("tools", ["powerbi"]),
    "Excel": ("tools", ["ms excel", "microsoft excel"]),
    "n8n": ("tools", []),
    "Zapier": ("tools", []),
    "Make.com": ("tools", ["integromat"]),
    "Twilio": ("tools", []),
    "WhatsApp Business API": ("tools", ["whatsapp api", "whatsapp cloud api"]),
    "Stripe": ("tools", []),
    "Razorpay": ("tools", []),
    "Webflow": ("tools", []),
    "Airflow": ("tools", ["apache airflow"]),
    "dbt": ("tools", []),
    # cloud
    "AWS": ("cloud", ["amazon web services", "ec2", "s3", "lambda"]),
    "GCP": ("cloud", ["google cloud", "google cloud platform"]),
    "Azure": ("cloud", ["microsoft azure"]),
    "Firebase": ("cloud", []),
    "Vercel": ("cloud", []),
    "Netlify": ("cloud", []),
    "Heroku": ("cloud", []),
    "Supabase": ("cloud", []),
    "Cloudflare": ("cloud", []),
    # databases
    "PostgreSQL": ("databases", ["postgres", "postgresql", "psql"]),
    "MySQL": ("databases", []),
    "SQLite": ("databases", []),
    "MongoDB": ("databases", ["mongo"]),
    "Redis": ("databases", []),
    "Elasticsearch": ("databases", ["elastic search", "opensearch"]),
    "DynamoDB": ("databases", []),
    "Cassandra": ("databases", []),
    "Snowflake": ("databases", []),
    "BigQuery": ("databases", []),
    "Oracle": ("databases", []),
    "SQL Server": ("databases", ["mssql", "ms sql"]),
    "Pinecone": ("databases", []),
    "ChromaDB": ("databases", ["chroma"]),
    "pgvector": ("databases", []),
    # ML / AI
    "Machine Learning": ("ml_ai", ["ml"]),
    "Deep Learning": ("ml_ai", ["dl"]),
    "NLP": ("ml_ai", ["natural language processing"]),
    "Computer Vision": ("ml_ai", ["cv", "opencv"]),
    "LLMs": (
        "ml_ai",
        [
            "llm",
            "large language models",
            "large language model",
            "genai",
            "generative ai",
            "gen ai",
        ],
    ),
    "RAG": ("ml_ai", ["retrieval augmented generation", "retrieval-augmented generation"]),
    "Prompt Engineering": ("ml_ai", []),
    "LangChain": ("ml_ai", []),
    "LlamaIndex": ("ml_ai", []),
    "OpenAI API": ("ml_ai", ["openai", "gpt-4", "chatgpt api"]),
    "Hugging Face": ("ml_ai", ["huggingface", "transformers"]),
    "PyTorch": ("ml_ai", ["torch"]),
    "TensorFlow": ("ml_ai", ["tf", "keras"]),
    "scikit-learn": ("ml_ai", ["sklearn", "scikit learn"]),
    "XGBoost": ("ml_ai", []),
    "Data Analysis": ("ml_ai", ["data analytics"]),
    "Statistics": ("ml_ai", []),
    "AI Agents": ("ml_ai", ["agentic ai", "ai agent", "autonomous agents"]),
    "Chatbots": ("ml_ai", ["chatbot", "conversational ai"]),
    "MLOps": ("ml_ai", []),
    # soft
    "Communication": ("soft", ["communication skills"]),
    "Leadership": ("soft", ["team lead", "led a team"]),
    "Teamwork": ("soft", ["collaboration"]),
    "Problem Solving": ("soft", ["problem-solving"]),
    "Project Management": ("soft", []),
    "Client Management": ("soft", ["client handling", "stakeholder management"]),
}

# Aliases that are too ambiguous to match on their own in free text.
_AMBIGUOUS = {
    "go",
    "py",
    "ts",
    "js",
    "rest",
    "node",
    "spring",
    "ml",
    "dl",
    "cv",
    "tf",
    "mongo",
    "chroma",
    "lambda",
    "s3",
    "ec2",
    "transformers",
    "rails",
    "vue",
    "openai",
}
_CASE_SENSITIVE = {
    "go": "Go",
    "ts": "TS",
    "js": "JS",
    "ml": "ML",
    "dl": "DL",
    "cv": "CV",
    "tf": "TF",
    "s3": "S3",
    "ec2": "EC2",
}


def category_of(skill: str) -> str | None:
    entry = _VOCAB.get(canonical(skill) or "")
    return entry[0] if entry else None


@cache
def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for name, (_cat, aliases) in _VOCAB.items():
        index[name.lower()] = name
        for alias in aliases:
            index[alias.lower()] = name
    return index


def canonical(skill: str) -> str | None:
    """Canonical name for a skill string, or None if it isn't in the vocabulary."""
    return _alias_index().get(skill.strip().lower())


def normalize_skill(skill: str) -> str:
    """Canonical name if known, else the trimmed original (unknown skills are kept)."""
    return canonical(skill) or skill.strip()


def _pattern(term: str) -> str:
    # Boundaries that treat + # . as part of a token (C++, C#, .NET, Node.js).
    return rf"(?<![\w+#.]){re.escape(term)}(?![\w+#])"


@cache
def _matchers() -> list[tuple[re.Pattern[str], str]]:
    out: list[tuple[re.Pattern[str], str]] = []
    for term, name in _alias_index().items():
        if term in _AMBIGUOUS:
            exact = _CASE_SENSITIVE.get(term)
            if exact:  # only match the exact-case acronym form, e.g. "ML" not "ml"
                out.append((re.compile(_pattern(exact)), name))
            continue
        out.append((re.compile(_pattern(term), re.IGNORECASE), name))
    # longest terms first so "React Native" wins over "React"
    out.sort(key=lambda item: -len(item[0].pattern))
    return out


def find_skills(text: str) -> list[str]:
    """Canonical skills mentioned in `text`, in first-seen order."""
    found: dict[str, int] = {}
    for pattern, name in _matchers():
        m = pattern.search(text)
        if m and name not in found:
            found[name] = m.start()
    # "React Native" also contains "React": keep both only if React appears on its own
    return [name for name, _ in sorted(found.items(), key=lambda kv: kv[1])]


def skill_in_text(skill: str, text: str) -> bool:
    """Is this (possibly unknown) skill evidenced in text? Alias-aware for known skills."""
    name = canonical(skill)
    if name is None:
        return re.search(_pattern(skill.strip()), text, re.IGNORECASE) is not None
    return (
        name in find_skills(text)
        or re.search(_pattern(skill.strip()), text, re.IGNORECASE) is not None
    )


def all_skills() -> dict[str, str]:
    """canonical name → category."""
    return {name: cat for name, (cat, _a) in _VOCAB.items()}
