"""
CAM IR processing module.

This module handles the processing of CAM IR (Intermediate Representation) JSON
into G-code, including validation, planning, and post-processing.
"""

import glob
import json
import os
import sys
from datetime import datetime
from typing import Dict, Optional, Tuple, List, Any, TYPE_CHECKING

from ai_config import AI_OUTPUT_DIR, KEEP_GENERATED_PROGRAMS

# Diameter difference (IR units) above which the tool table value replaces the IR's
DIAMETER_TOLERANCE = 0.01

# Import CAM IR modules
if TYPE_CHECKING:
    from cam_ir.ir_types import (
        Operation, DrillOp, Profile2DOp, Pocket2DOp, FaceOp,
        EngraveOp, TextOp, BoringOp, TapOp, ThreadOp
    )

try:
    # qtvcp runs under the system Python with the venv on PYTHONPATH, which
    # skips .pth files, so the editable cam_ir install isn't found. Put the
    # package source (libs/cam_ir, next to this file) on the path directly.
    cam_ir_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs", "cam_ir")
    if os.path.isdir(cam_ir_path) and cam_ir_path not in sys.path:
        sys.path.insert(0, cam_ir_path)

    from cam_ir.schema import load_ir
    from cam_ir.validate import validate_ir, ValidationError
    from cam_ir.ir_types import (
        Operation, DrillOp, Profile2DOp, Pocket2DOp, FaceOp,
        EngraveOp, TextOp, BoringOp, TapOp, ThreadOp
    )
    from cam_ir.planner.drill import plan_drill
    from cam_ir.planner.profile import plan_profile
    from cam_ir.planner.pocket import plan_pocket
    from cam_ir.planner.ops_2d import plan_face
    from cam_ir.planner.engrave import plan_engrave
    from cam_ir.planner.text import plan_text
    from cam_ir.planner.bore import plan_bore
    from cam_ir.planner.tap import plan_tap
    from cam_ir.planner.thread import plan_thread
    from cam_ir.planner.optimize import optimize_toolpath
    from cam_ir.planner.apply_feedspeed import apply_calculated_feedspeeds
    from cam_ir.planner.transform import transform_moves
    from cam_ir.post.linuxcnc import post_process_operations
    CAM_IR_AVAILABLE = True
except ImportError as e:
    CAM_IR_AVAILABLE = False
    CAM_IR_ERROR = str(e)
    # Define dummy types for type hints when imports fail
    Operation = None
    DrillOp = None
    Profile2DOp = None
    Pocket2DOp = None
    FaceOp = None
    EngraveOp = None
    TextOp = None
    BoringOp = None
    TapOp = None
    ThreadOp = None


def operation_depth(op) -> Optional[float]:
    """How deep an operation cuts below its top (IR units), or None if it can't be told"""
    top = getattr(op, "top_z", None)
    bottom = getattr(op, "bottom_z", None)
    if top is not None and bottom is not None:
        return abs(top - bottom)
    depth = getattr(op, "depth", None)
    return abs(depth) if depth is not None else None


class CAMIRProcessor:
    """Processes CAM IR JSON into G-code"""
    
    def __init__(self, log_callback=None, progress_callback=None):
        """
        Initialize the CAM IR processor.
        
        Args:
            log_callback: Optional callback function for logging messages
            progress_callback: Optional callback function for progress updates (percent: int)
        """
        self.log = log_callback if log_callback else (lambda msg: None)
        self.progress = progress_callback if progress_callback else (lambda p: None)
    
    def extract_json_from_response(self, response_text: str) -> Optional[dict]:
        """
        Extract JSON from AI response text.
        
        Args:
            response_text: The raw response text from the AI
            
        Returns:
            Parsed JSON dict or None if extraction fails
        """
        json_text = response_text.strip()
        
        # Remove markdown code blocks if present
        if "```json" in json_text:
            json_text = json_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in json_text:
            # Generic code block
            json_text = json_text.split("```", 1)[1].split("```", 1)[0].strip()
        
        # Try to parse JSON
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            start_idx = json_text.find("{")
            end_idx = json_text.rfind("}") + 1
            if start_idx >= 0 and end_idx > start_idx:
                json_text = json_text[start_idx:end_idx]
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    return None
            return None
    
    def plan_operation(self, op: "Operation", ir_obj) -> List[Any]:
        """
        Plan a single operation and return moves.
        
        Args:
            op: The operation to plan
            ir_obj: The CAM IR object containing tools and settings
            
        Returns:
            List of moves for the operation
        """
        tool_map = {tool.tool: tool for tool in ir_obj.tools}
        tool = tool_map[op.tool]
        
        if isinstance(op, DrillOp):
            moves = plan_drill(op, ir_obj.safe_z, ir_obj.safe_z)
        elif isinstance(op, Profile2DOp):
            moves = plan_profile(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, Pocket2DOp):
            moves = plan_pocket(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, FaceOp):
            moves = plan_face(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, EngraveOp):
            moves = plan_engrave(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, TextOp):
            moves = plan_text(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, BoringOp):
            moves = plan_bore(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, TapOp):
            moves = plan_tap(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        elif isinstance(op, ThreadOp):
            moves = plan_thread(op, tool, ir_obj.safe_z, ir_obj.clearance_z)
        else:
            raise ValueError(f"Unknown operation type: {op.op}")
        
        # Apply WCS transformation if specified
        wcs_index = getattr(op, 'wcs', None)
        if wcs_index is not None:
            wcs = ir_obj.get_wcs(wcs_index)
            if wcs.rotation_deg != 0.0 or wcs.origin != [0.0, 0.0, 0.0]:
                moves = transform_moves(moves, wcs)
        
        return moves
    
    def check_against_machine(self, ir, tools: Optional[Dict[int, Optional[float]]],
                              machine_units: str, max_rpm: Optional[float],
                              flute_lengths: Optional[Dict[int, float]] = None) -> List[str]:
        """
        Reconcile the IR with the machine. Tool diameters from the tool table
        replace the IR's (the model guesses; the table is the physical tool).

        Args:
            ir: Loaded CAMIR object (modified in place)
            tools: Usable tool numbers -> trusted diameter in machine units (None if unknown),
                   or None to skip tool checks
            machine_units: "mm" or "inch"
            max_rpm: Spindle maximum, or None to skip the check
            flute_lengths: Tool number -> flute length in machine units, for tools linked to a
                           catalog entry; no operation may cut deeper than that

        Returns:
            Error messages (empty if the IR fits the machine)
        """
        errors = []
        if flute_lengths:
            ir_units = ir.units.value if hasattr(ir.units, "value") else str(ir.units)
            scale = 1.0 if ir_units == machine_units else (1 / 25.4 if machine_units == "mm" else 25.4)
            for i, op in enumerate(ir.ops):
                limit = flute_lengths.get(getattr(op, "tool", None))
                depth = operation_depth(op)
                if limit and depth is not None and depth > limit * scale + 1e-6:
                    errors.append(f"Operation {i} ({op.op}) cuts {depth:g} deep, but T{op.tool}'s flutes are only "
                                  f"{limit * scale:g} long. Use a longer tool or a shallower cut.")
        if tools is not None:
            ir_units = ir.units.value if hasattr(ir.units, "value") else str(ir.units)
            scale = 1.0
            if ir_units != machine_units:
                scale = 1 / 25.4 if machine_units == "mm" else 25.4  # machine units -> IR units
            for tool in ir.tools:
                if tool.tool not in tools:
                    errors.append(f"Tool {tool.tool} is not a usable tool in the machine's tool table "
                                  f"(available: {', '.join(f'T{n}' for n in sorted(tools)) or 'none'}).")
                    continue
                table_diameter = tools[tool.tool]
                if table_diameter is None:
                    continue
                table_diameter *= scale
                if abs(tool.diameter - table_diameter) > DIAMETER_TOLERANCE:
                    self.log(f"[CAM] T{tool.tool}: using tool table diameter {table_diameter:g} "
                             f"instead of {tool.diameter:g} from the AI.")
                    tool.diameter = table_diameter
        if max_rpm is not None:
            for i, op in enumerate(ir.ops):
                if op.rpm > max_rpm:
                    errors.append(f"Operation {i} ({op.op}): rpm {op.rpm} is above the spindle maximum of {max_rpm:g}.")
        return errors

    def prune_outputs(self, output_dir: str, keep: int = KEEP_GENERATED_PROGRAMS):
        """Delete all but the newest `keep` generated programs (and their IR JSON)"""
        programs = sorted(glob.glob(os.path.join(output_dir, "ai_toolpath_*.ngc")), key=os.path.getmtime)
        for path in programs[:-keep] if keep > 0 else programs:
            for victim in (path, path[:-4] + ".json"):
                try:
                    os.remove(victim)
                except OSError:
                    pass

    def process_ir_to_gcode(
        self,
        ir_data: dict,
        output_dir: str = AI_OUTPUT_DIR,
        tools: Optional[Dict[int, Optional[float]]] = None,
        machine_units: str = "mm",
        max_rpm: Optional[float] = None,
        flute_lengths: Optional[Dict[int, float]] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Process CAM IR data into G-code.
        
        Args:
            ir_data: The CAM IR JSON data as a dict
            output_dir: Directory to save output files
            tools: Usable tools and trusted diameters, see check_against_machine
            machine_units: "mm" or "inch"
            max_rpm: Spindle maximum rpm
            
        Returns:
            Tuple of (gcode_filepath, ir_filepath, error_message)
            Returns (None, None, error_message) on failure
        """
        if not CAM_IR_AVAILABLE:
            return None, None, f"CAM IR library not available: {CAM_IR_ERROR}"
        
        try:
            # Generate unique filenames
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"ai_toolpath_{timestamp}.ngc"
            output_path = os.path.expanduser(os.path.join(output_dir, output_filename))
            ir_filename = f"ai_toolpath_{timestamp}.json"
            ir_file_path = os.path.expanduser(os.path.join(output_dir, ir_filename))
            
            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Save the JSON IR file
            with open(ir_file_path, "w") as f:
                json.dump(ir_data, f, indent=2)
            self.log(f"[INFO] Saved CAM IR JSON to: {ir_file_path}")
            self.progress(30)
            
            # Load and validate IR
            try:
                ir = load_ir(ir_file_path)
                self.log("[INFO] CAM IR JSON loaded successfully.")
            except Exception as e:
                return None, None, f"Failed to load CAM IR: {e}"
            
            self.progress(50)
            
            # Apply calculated feeds/speeds if requested
            try:
                ir = apply_calculated_feedspeeds(ir)
                self.log("[INFO] Applied calculated feeds/speeds if available.")
            except Exception as e:
                self.log(f"[WARN] Could not apply calculated feeds/speeds: {e}")
            
            # Validate IR
            try:
                validate_ir(ir)
                self.log("[INFO] CAM IR validation passed.")
            except ValidationError as e:
                return None, None, f"CAM IR validation failed: {e}"

            # Check against the machine (tool table, spindle limit)
            machine_errors = self.check_against_machine(ir, tools, machine_units, max_rpm, flute_lengths)
            if machine_errors:
                return None, None, "CAM IR doesn't fit the machine: " + "\n".join(machine_errors)
            
            self.progress(60)
            
            # Plan operations
            operation_moves = []
            for i, op in enumerate(ir.ops):
                try:
                    moves = self.plan_operation(op, ir)
                    # Optimize moves for this operation
                    moves = optimize_toolpath(moves)
                    operation_moves.append(moves)
                    self.log(f"[INFO] Planned operation {i+1}/{len(ir.ops)}: {op.op}")
                except Exception as e:
                    return None, None, f"Failed to plan operation {i+1} ({op.op}): {e}"
            
            self.progress(80)
            
            # Post-process to G-code
            try:
                gcode = post_process_operations(ir, operation_moves)
                self.log("[INFO] G-code generated successfully.")
            except Exception as e:
                import traceback
                return None, None, f"Failed to post-process operations: {e}\n{traceback.format_exc()}"
            
            # Write G-code file
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(gcode)
            self.log(f"[INFO] G-code written to: {output_path}")
            self.prune_outputs(os.path.dirname(output_path))
            
            self.progress(100)
            
            return output_path, ir_file_path, None
            
        except Exception as e:
            import traceback
            return None, None, f"Failed to process CAM IR: {str(e)}\n{traceback.format_exc()}"

