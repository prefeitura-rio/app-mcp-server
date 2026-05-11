from dataclasses import dataclass


@dataclass(frozen=True)
class CommonWorkflowConfig:
    address_required: bool = True
    reference_point_required: bool = False
    identification_required: bool = False
    occurrence_origin_code: str = "28"
