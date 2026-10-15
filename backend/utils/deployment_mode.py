"""Deployment-only pause for the first configuration import and verification."""
import os


def maintenance_enabled() -> bool:
    return os.environ.get('DEPLOYMENT_MAINTENANCE', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def ensure_execution_enabled() -> None:
    if maintenance_enabled():
        raise ValueError('deployment_maintenance: trading and account mutations are paused until configuration verification is complete')
