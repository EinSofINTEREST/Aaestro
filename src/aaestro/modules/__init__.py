"""Module system — skillsets, promptsets, memorysets, skillpacks, and the registry.

모듈 시스템. 솔루션은 프레임워크 코드 수정이 아니라 이 모듈들의 조립으로 구성된다.
"""

from aaestro.modules.compatibility import (
    check_promptset_templates,
    check_skillset_env,
    check_tool_namespaces,
)
from aaestro.modules.memoryset import (
    LoadedMemoryset,
    MemoryKind,
    MemoryScope,
    MemorysetLoader,
    MemorysetManifest,
    check_attachment_scope,
)
from aaestro.modules.promptset import (
    DEFAULT_TEMPLATE,
    LoadedPromptset,
    PromptsetLoader,
    PromptsetManifest,
)
from aaestro.modules.registry import (
    ModulePath,
    ModuleRegistry,
    RegistryRoots,
)
from aaestro.modules.skillpack import (
    LoadedSkillpack,
    PackedSkill,
    SkillpackLoader,
    SkillpackManifest,
)
from aaestro.modules.skillset import (
    LoadedSkill,
    LoadedSkillset,
    SkillsetLoader,
    SkillsetManifest,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "LoadedMemoryset",
    "LoadedPromptset",
    "LoadedSkill",
    "LoadedSkillpack",
    "LoadedSkillset",
    "MemoryKind",
    "MemoryScope",
    "MemorysetLoader",
    "MemorysetManifest",
    "ModulePath",
    "ModuleRegistry",
    "PackedSkill",
    "PromptsetLoader",
    "PromptsetManifest",
    "RegistryRoots",
    "SkillpackLoader",
    "SkillpackManifest",
    "SkillsetLoader",
    "SkillsetManifest",
    "check_attachment_scope",
    "check_promptset_templates",
    "check_skillset_env",
    "check_tool_namespaces",
]
