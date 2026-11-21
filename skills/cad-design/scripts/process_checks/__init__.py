# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Process-dispatched manufacturability checks for record version 2 parts (cad-design).

Each made component of each part is judged per route operation by the handler of that operation's kind,
against criteria from the operation's capability-profile calibrations for the component's material, and
against the recorded plan (orientation, supports, setup, stock, allowances, inspection). Purchased items get
only purchase checks. A kind with no handler is a coverage gap: UNKNOWN, never a pass and never FDM checks.

Policy this package applies (a plugin rule, not a product fact):
- Unknown never becomes pass: a missing calibration, plan entry or measurement leaves its obligation UNKNOWN.
- No universal limit: wall, overhang, bridge, hole, kerf, radius, draft and tolerance criteria are
  calibrations of the actual equipment (`manufacturing_intent.capability_limit`); machine parameters,
  nominal resolution and general-tolerance labels never answer them. For FDM overhang, bridge, wall, small
  feature and small hole, a missing calibration falls back to the guideline sets of the profile and material
  (cad-record guidelines.py, given as `assess(catalog=)`). A guideline fail fails; a guideline pass counts only
  for GUIDELINE_PASS_CLASSES and is UNKNOWN for every other service class until calibrated or waived.
  While the calibration flag is off (guidelines.CALIBRATION_FLAG, the release behaviour) a recorded FDM calibration is
  ignored and a guideline pass counts in every service class.
  block_like and build-volume fit stay form_check diagnostics.
- Supports: on reachable non-critical surfaces they are accepted; an overhang beyond the unsupported limit
  is a planning finding when a support or bridge plan covers it; unplanned contact on a precision or running
  surface is a MAJOR finding; soluble or interface supports alone establish no precision; an exception needs
  a later finishing operation, an allowance, removal access and inspection, and stays physically unvalidated.
- Laser stock: halogenated polymers (PVC and similar) are rejected; anything else must be an identified
  grade approved for the machine, thickness, coating and extraction, or it is blocked pending identification.
- Optical or transparent parts: FDM and resin are eliminated; a purchased transparent part with traceable
  optical criteria, or identified transparent stock cut or machined, is the route.
- Geometry PASS covers only the measured facts named in its coverage; coupons, slicer or CAM review, print
  trials and first-article inspection are physical evidence the ledger holds (they are named as findings).

Public API: `expected(record)`, `assess(record, geometry, today=, catalog=)` -> `Assessment` (`outcomes`, `findings`,
`complete`, `result`, `packet(dependencies)`), `probe_plan(record, PN, component, catalog=)`, `component_contexts`, `guideline_scope`, `Geometry`,
`TABLE`, `owned`, `NotAssessable`, `HandlerError`, `PRODUCER`, `PROBE_FORMAT`, `GUIDELINE_PASS_CLASSES`, `guidelines`.
"""

from .core import (GUIDELINE_PASS_CLASSES, IMPLICIT, PROBE_FORMAT, Geometry, component_contexts, guideline_scope, guidelines,
                   intent)
from .dispatch import (PRODUCER, TABLE, Assessment, HandlerError, NotAssessable, assess, code_sha256, expected,
                       handler_for, owned, probe_plan)

__all__ = ["GUIDELINE_PASS_CLASSES", "IMPLICIT", "PROBE_FORMAT", "PRODUCER", "TABLE", "Assessment", "Geometry", "HandlerError",
           "NotAssessable", "assess", "code_sha256", "component_contexts", "expected", "guideline_scope", "guidelines",
           "handler_for", "intent", "owned", "probe_plan"]
