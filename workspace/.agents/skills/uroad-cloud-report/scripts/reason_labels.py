"""Shared, requirement-aligned labels for uroad reason codes."""
from __future__ import annotations

REASON_LABELS = {
    "speed_gate": "车速超出 uroad 工作范围",
    "perception_gate": "感知状态不满足运行条件",
    "apa_gate": "当前处于泊车模式",
    "gear_gate": "当前不是 D 挡",
    "camera_image_empty": "相机图像为空",
    "trajectory_too_few_points": "轨迹点数量不足",
    "image_processing_failed": "图像处理失败",
    "point_cloud_too_few_points": "点云有效点数不足",
    "expected_inputs_mismatch": "收到的推理输入数量不正确",
    "passthrough_extract_failed": "透传数据解析失败",
    "inference_output_extract_failed": "推理输出解析失败",
    "bev_splatting_failed": "BEV 重建失败",
    "lidar_timestamp_decode_failed": "雷达时间戳解析失败",
    "n_passthrough_decode_failed": "透传点数解析失败",
    "traj_meta_decode_failed": "轨迹元数据解析失败",
    "means3d_decode_failed": "推理输出关键字段解析失败",
    "back_trajectory_empty": "后段轨迹为空",
    "inference_data_empty": "推理结果为空",
    "uroad_graph_overrun": "uroad 调度图执行超时",
    "trajectory_data_timeout": "轨迹数据获取超时",
    "temporal_gap": "处理时间间隔过大",
    "bumppoint_null": "地图融合输入缺少 BumpPoint",
    "edsg_context_drop": "EDSG 调度图丢弃处理 context",
}

MAX_REASON_CODE_LENGTH = 500


def normalize_reason_code(code: object) -> str:
    """Bound the engineering code before adding human-facing punctuation."""
    return str(code).strip()[:MAX_REASON_CODE_LENGTH]


def reason_label(code: object) -> str:
    """Return human-readable Chinese text while retaining the engineering code."""
    text = normalize_reason_code(code)
    label = REASON_LABELS.get(text)
    return f"{label}（{text}）" if label else f"未收录原因（{text}）"
