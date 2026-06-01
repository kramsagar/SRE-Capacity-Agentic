"""
agent/skill_loader.py

Loads LLM skill files (.md) from the skills/ directory.
Provides the skill content as context strings for LLM prompts.
"""

from pathlib import Path


class SkillLoader:
    def __init__(self, skills_root: Path):
        self.root = skills_root

    def load(self, relative_path: str) -> str:
        """Load a skill file by relative path, e.g. 'capacity/capacity_analysis.md'"""
        path = self.root / relative_path
        if not path.exists():
            return f"[Skill not found: {relative_path}]"
        return path.read_text()

    def load_service_profiles(self, services: list[str]) -> str:
        """Load and concatenate all available service profile.md files."""
        parts = []
        for svc in services:
            profile_path = self.root / "services" / svc / "profile.md"
            if profile_path.exists():
                parts.append(f"## {svc}\n{profile_path.read_text()}")
        return "\n\n".join(parts) if parts else "(no service profiles found)"

    def load_all_capacity_skills(self) -> str:
        """Load all capacity skill files concatenated."""
        capacity_dir = self.root / "capacity"
        if not capacity_dir.exists():
            return ""
        parts = []
        for md_file in sorted(capacity_dir.glob("*.md")):
            parts.append(f"# {md_file.stem}\n{md_file.read_text()}")
        return "\n\n---\n\n".join(parts)
