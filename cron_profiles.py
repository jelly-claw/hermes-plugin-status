"""Per-profile cron execution shims for group agents.

Hermes's cron ticker always runs agent-mode jobs under the process-default
HERMES_HOME (main Jelly). These two small wraps — installed by the status
plugin's register() in the gateway process only — make jobs created by a
group agent run AS that agent:

1. create_job stamp: a job created during a profile-scoped agent turn gets
   ``profile: <name>`` recorded on it (the cron store itself is the default
   profile's ~/.hermes/cron/jobs.json in all cases — cron.jobs resolves its
   paths at import time, so the single ticker sees every job).
2. run_job scope: when a due job carries ``profile``, its execution (prompt
   build + agent run) happens inside that profile's HERMES_HOME override and
   secret scope — same seam the gateway multiplexer uses for inbound turns.
   Delivery runs unscoped afterwards (run_one_job calls _deliver_result
   outside run_job), which is required: the profile's config has the status
   platform explicitly disabled.

Both wraps are defensive: on any mismatch with a future Hermes version they
log a warning and fall back to stock behavior (the job still runs, as main
Jelly, and still delivers to its origin chat).
"""

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_installed = False


def _current_profile_name() -> Optional[str]:
    """Name of the profile the current context is scoped to, else None."""
    try:
        from hermes_constants import get_hermes_home, get_default_hermes_root
        home = Path(get_hermes_home()).resolve()
        root = Path(get_default_hermes_root()).resolve()
        if home == root:
            return None
        if home.parent.name == "profiles" and home.parent.parent == root:
            return home.name
    except Exception:
        pass
    return None


def install() -> None:
    """Install both shims. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    _install_create_stamp()
    _install_run_scope()


def _install_create_stamp() -> None:
    try:
        import cron.jobs as cron_jobs
        orig = cron_jobs.create_job
        if getattr(orig, "_status_profile_shim", False):
            return

        def create_job(*args, **kwargs):
            job = orig(*args, **kwargs)
            try:
                prof = _current_profile_name()
                if prof and isinstance(job, dict) and job.get("id"):
                    cron_jobs.update_job(job["id"], {"profile": prof})
                    job["profile"] = prof
                    logger.info(
                        "Status cron shim: job %s stamped profile=%r", job["id"], prof
                    )
            except Exception as e:
                logger.warning("Status cron shim: profile stamp failed: %s", e)
            return job

        create_job._status_profile_shim = True
        create_job.__wrapped__ = orig
        cron_jobs.create_job = create_job

        # tools.cronjob_tools binds create_job by value at ITS import time
        # (`from cron.jobs import create_job`). Normally it imports after us
        # (lazily, on first tool use) and picks up the wrap; rebind defensively
        # in case it is already loaded.
        m = sys.modules.get("tools.cronjob_tools")
        if m is not None and getattr(m, "create_job", None) is orig:
            m.create_job = create_job

        logger.info("Status cron shim: create_job profile stamping installed")
    except Exception as e:
        logger.warning("Status cron shim: could not wrap create_job: %s", e)


def _enter_profile_scope(profile: str, job_id: str):
    """Enter HERMES_HOME + secret scope for *profile*.

    Returns a zero-arg cleanup callable, or None when the scope could not be
    entered (missing profile dir, import failure) — caller then runs the job
    unscoped. Never raises.
    """
    try:
        from hermes_cli.profiles import get_profile_dir
        pdir = Path(get_profile_dir(profile))
        if not pdir.is_dir():
            logger.warning(
                "Cron job %s: profile %r has no directory — running as default",
                job_id, profile,
            )
            return None
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        home_token = set_hermes_home_override(str(pdir))
        secret_cleanup = None
        try:
            from agent.secret_scope import (
                build_profile_secret_scope,
                set_secret_scope,
                reset_secret_scope,
            )
            # profile_home stamp is required: unstamped means "process home", and inside a
            # bound scope set_secret_scope would switch the whole scope back to ~/.hermes.
            secret_token = set_secret_scope(build_profile_secret_scope(pdir), profile_home=str(pdir))
            secret_cleanup = lambda: reset_secret_scope(secret_token)
        except Exception as e:
            logger.debug("Cron job %s: no secret scope (%s)", job_id, e)

        def _cleanup():
            try:
                if secret_cleanup is not None:
                    secret_cleanup()
            finally:
                reset_hermes_home_override(home_token)

        return _cleanup
    except Exception as e:
        logger.warning(
            "Cron job %s: entering profile scope %r failed (%s) — running as default",
            job_id, profile, e,
        )
        return None


def _install_run_scope() -> None:
    try:
        import cron.scheduler as cron_scheduler
        orig = cron_scheduler.run_job
        if getattr(orig, "_status_profile_shim", False):
            return

        def run_job(job, *args, **kwargs):
            profile = ""
            job_id = ""
            try:
                profile = str((job or {}).get("profile") or "").strip()
                job_id = str((job or {}).get("id") or "?")
            except Exception:
                profile = ""
            if profile and profile != "default":
                cleanup = _enter_profile_scope(profile, job_id)
                if cleanup is not None:
                    logger.info("Cron job %s: running under profile %r", job_id, profile)
                    try:
                        return orig(job, *args, **kwargs)
                    finally:
                        cleanup()
            return orig(job, *args, **kwargs)

        run_job._status_profile_shim = True
        run_job.__wrapped__ = orig
        cron_scheduler.run_job = run_job
        logger.info("Status cron shim: run_job profile scoping installed")
    except Exception as e:
        logger.warning("Status cron shim: could not wrap run_job: %s", e)
