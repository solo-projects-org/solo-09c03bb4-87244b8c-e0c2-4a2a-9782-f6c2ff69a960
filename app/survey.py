# -*- coding: utf-8 -*-
"""兼容性别名模块：断面/测深逻辑的规范实现位于 :mod:`app.sections`。"""

from .sections import *  # noqa: F401,F403
from .sections import (  # noqa: F401
    SECTION_OK,
    SECTION_PENDING,
    PointDepth,
    SectionReport,
    SectionResult,
    Sounding,
    SoundingRow,
    SoundingValidationError,
    ZeroInfo,
    build_section_report,
    build_zero_index,
    changed_zero_points,
    compute_section,
    compute_section_results,
    compute_sections,
    evaluate_sections,
    find_changed_zeros,
    parse_csv,
    parse_sounding_csv,
    parse_soundings,
    recompute_sections,
    soundings_from_dicts,
    zero_qualification,
)
