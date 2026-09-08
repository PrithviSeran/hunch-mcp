"""Bounded observations and explicit recovery for a caller-owned Hunch session."""
import re
import time
import uuid

from .errors import ApprovalDenied, HunchError
from .targets import TargetRegistry, process_key


class Capabilities:
    def __init__(self, owner):
        self.owner = owner
        self.registry = TargetRegistry()
        self.plans = {}
        self.history = []

    def targets(self, app="", window="", select=False, inventory="running"):
        if inventory == "installed":
            if select or window:
                raise HunchError("installed inventory cannot select a running window; use inventory='running'")
            from .targets import installed_apps
            return installed_apps(app)
        if inventory != "running":
            raise HunchError("inventory must be running or installed")
        from . import local_mac as native
        apps = [{**a, **native._running_identity(a["pid"])} for a in native.ax.list_apps()]
        if not app:
            return {"running": apps, "scope": "running applications; not installed inventory"}
        from .targets import select_running
        identity = native._resolve_app(app) if app.startswith("pid:") else select_running(app, apps)
        if not identity:
            return {"status": "blocked", "reason": "no matching running application", "app": app}
        root = native.AXUIElementCreateApplication(identity["pid"])
        observations, errors = native.ax.discover_windows(root)
        windows = self.registry.bind(identity, observations)
        if select:
            selected = self.registry.window(window) if window else None
            session = self.owner._computer.session
            session._invalidate_refs()
            session._selected_window = (process_key(identity), selected.element) if selected else None
            self.owner._computer.app = f"pid:{identity['pid']}"
            self.owner._aimed_app = self.owner._computer.app
        return {"status": "available" if windows else "unavailable_in_current_state",
                "target": identity, "generation": self.registry.generation,
                "trusted": bool(native.AXIsProcessTrusted()), "ax_errors": errors,
                "windows": [{"handle": w.handle, "provenance": w.provenance,
                             "title": str(native.ax.get_attr(w.element, "AXTitle") or "")[:200]}
                            for w in windows], "bounded": True, "window_limit": 64}

    def inspect(self, app, operation="observe"):
        from . import local_mac as native
        result = self.targets(app)
        if "target" not in result:
            return result
        identity = result["target"]
        result.update(operation=operation, mechanism="ax", effect_verified=False,
                      background_tested=False, observed_at=time.time(), recoveries=[])
        # Available windows do not establish coverage of arbitrary user operations.
        result["operation_status"] = "unknown"
        if not result["trusted"]:
            result["status"] = "permission_denied"
        command = native._proc_cmdline(identity["pid"])
        port = re.search(r"(?:^|\s)--remote-debugging-port=(\d+)(?:\s|$)", command)
        if port:
            result["recoveries"].append(self._plan(identity, operation, "attach_cdp", port=int(port[1])))
        embedded = native._embedded_chromium(identity["pid"])
        if embedded and result["trusted"]:
            result["recoveries"].append(self._plan(identity, operation, "enable_ax"))
            result["recoveries"].append(self._plan(identity, operation, "restart_ax"))
        if embedded and not port:
            result["recoveries"].append(self._plan(identity, operation, "restart_debug"))
        result["history"] = [h for h in self.history if h["target"] == process_key(identity)][-8:]
        attempted = {h["action"] for h in result["history"]}
        result["recoveries"] = [p for p in result["recoveries"] if p["action"] != "enable_ax" or "enable_ax" not in attempted]
        return result

    def _constraints(self):
        return (self.owner.background_only, self.owner.simultaneous,
                self.owner._computer.app, getattr(self.owner._computer.session, "_selected_window", None))

    def _plan(self, identity, operation, action, **options):
        now = time.monotonic()
        self.plans = {k: p for k, p in self.plans.items() if p["expires"] > now}
        if len(self.plans) >= 64:
            self.plans.pop(next(iter(self.plans)))
        plan_id = uuid.uuid4().hex
        plan = dict(target=dict(identity), operation=operation, action=action, options=options,
                    expires=now + 120, constraints=self._constraints())
        self.plans[plan_id] = plan
        return {"plan_id": plan_id, "action": action, "expires_in_seconds": 120,
                "process_restart": action.startswith("restart"),
                "profile": "preserve verified profile; refuse unknown custom profile" if action.startswith("restart") else "unchanged"}

    def recover(self, plan_id, target_id=""):
        from . import local_mac as native
        plan = self.plans.pop(plan_id, None)
        if not plan or plan["expires"] <= time.monotonic():
            raise HunchError("recovery plan expired or was already attempted; inspect capabilities again")
        identity = plan["target"]
        if (process_key(native._running_identity(identity["pid"])) != process_key(identity)
                or self._constraints() != plan["constraints"]):
            raise HunchError("recovery target or host constraints changed; inspect capabilities again")
        action = plan["action"]
        result = dict(status="performed_unverified", target=process_key(identity),
                      action=action, operation=plan["operation"], disturbances={})
        if action.startswith("restart"):
            gate = self.owner._gate
            if gate.enabled("app_to_front") and not gate.confirm_dialog(
                    f"Restart {identity['name']} gracefully for {action}? Unsaved work may need attention.",
                    category="app_lifecycle", detail=identity.get("path", ""), screen_approval=False):
                raise ApprovalDenied("recovery restart was not authorized")
        try:
            if action == "enable_ax":
                native._enable_manual_ax(native.AXUIElementCreateApplication(identity["pid"]))
                result["evidence"] = self.targets(f"pid:{identity['pid']}")
            elif action == "attach_cdp":
                result["evidence"] = self.owner.web.attach(identity.get("path") or identity["name"],
                                                          plan["options"]["port"], target_id,
                                                          expected_identity=identity)
            elif action in ("restart_ax", "restart_debug"):
                port = None
                if action == "restart_debug":
                    import socket
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        port = listener.getsockname()[1]
                message = native.launch_app(f"pid:{identity['pid']}",
                                            force_accessibility=action == "restart_ax", background=True,
                                            debugging_port=port)
                result["evidence"] = message
                result["disturbances"] = {"restart_requested": 1}
                if message.startswith(("REFUSED:", "failed")):
                    result["status"] = "blocked"
                elif port:
                    result["evidence"] = self.owner.web.attach(identity["path"], port, target_id)
            else:
                raise HunchError("unknown recovery action")
        except Exception as error:
            result.update(status="blocked", evidence=str(error))
        if isinstance(result.get("evidence"), dict) and result["evidence"].get("status") == "blocked":
            result["status"] = "blocked"
        self.history.append({k: result[k] for k in ("target", "action", "status")})
        self.history = self.history[-64:]
        return result
