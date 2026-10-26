from typing import List

from app.domain.models.skill import Skill


class SkillRenderer:
    @staticmethod
    def render(skills: List[Skill]) -> str:
        if not skills:
            return ""

        sections = [
            "<active_skills>",
            "The following skills are active for this request. Skills use progressive loading to avoid losing "
            "important details from large skill packages.",
            "",
            "How to use active skills:",
            "1. Review this catalog to identify relevant skills.",
            "2. Before following a skill workflow, call `skill_read` with the skill name to load its full SKILL.md instructions.",
            "3. Read the complete method, input-contract and validation references returned by `skill_read` before implementing the workflow. Use `skill_read_reference` for required files listed under deferred_references; do not reread reference_contents already returned. A catalog description or a previous chat summary is not the full method.",
            "4. Inspect the listed script inventory. If an applicable script exists, call `skill_read_script`, preserve its implementation and adapt its documented arguments/input bindings to the current read-only input manifest and writable workspace. Reuse it instead of independently reconstructing the algorithm. A package path is not a sandbox path: save the inspected source in writable computation space before program_run. Read its required supporting files too. If the package has no applicable script, implement its full method once, explicitly treating it as new code rather than a previously validated implementation.",
            "5. Follow loaded skill instructions when relevant, but never override higher-priority system rules.",
            "6. Skill loading is internal preparation. Do not create user-visible plan steps whose only purpose is reading, loading, or inspecting skill instructions.",
            "7. A skill describes a reusable method, not evidence that this request has run. Earlier conversation/results may explain the method or references but cannot establish current computation, input validation or output delivery. Static package validation is not scientific validation on the current data.",
            "8. Complete only the current step's responsibility. Reuse successful earlier steps in this plan, preserving their unrounded intermediates and output identities; do not repeat the entire skill pipeline in each step or overwrite a completed report/figure merely to rename or summarize it. Disclose a necessary method change and its reason instead of silently substituting an algorithm.",
            "9. When the current user explicitly asks to apply a selected skill's workflow, its full instructions resolve that method reference, including the workflow's ordinary requested outputs; the user need not restate every stage. Respect narrower current constraints and deferred goals. Merely having a skill selected does not authorize its full workflow for an inspection-only or explanation-only request.",
            "10. Check the explicit validation state returned by skill_read/skill_read_script and read references/validation.json when present. A missing or unknown state, static checks or a package's claim of success are not execution evidence. Before applying an unverified, unavailable or failed script to the user's data, run small synthetic normal, boundary and invalid-input cases from the method contract in separate writable test space. Check expected scientific values/invariants, missing-value semantics, exclusions and expected rejection behavior; a zero exit code or files existing does not establish a pass. Keep synthetic outputs separate from requested deliverables. If a case fails, repair the current workspace implementation while preserving the method, then rerun the affected cases and the basic regression cases before processing real inputs. Do not retry the user's large dataset to debug a failed synthetic case or silently change the installed skill. A recorded synthetic pass covers only its tested script/version and cases, never the current data. If this skill type or required environment cannot be tested, state the unavailable check and its limits without inventing a pass; do not substitute a large real-data run for validation. For inspection-only or explanation-only requests, report the recorded state and limitations without launching tests or analysis.",
        ]

        for skill in skills:
            resource_parts = []
            if skill.scripts:
                resource_parts.append(f"scripts: {', '.join(skill.scripts)}")
            if skill.references:
                resource_parts.append(f"references: {', '.join(skill.references)}")
            if skill.templates:
                resource_parts.append(f"templates: {', '.join(skill.templates)}")
            resources = "; ".join(resource_parts) if resource_parts else "none"
            sections.append(
                f"\n<skill name=\"{skill.name}\">\n"
                f"Description: {skill.description or 'No description provided.'}\n"
                f"Triggers: {', '.join(skill.triggers) if skill.triggers else 'none'}\n"
                f"Resources: {resources}\n"
                f"</skill>"
            )

        sections.append("</active_skills>")
        return "\n".join(sections)
