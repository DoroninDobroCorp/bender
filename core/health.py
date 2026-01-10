"""
Health Check Module for Parser Maker

Provides health check endpoints and system status monitoring.
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


logger = logging.getLogger(__name__)


class ComponentStatus(Enum):
    """Health status of a component"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Health status of a single component"""
    name: str
    status: ComponentStatus
    message: Optional[str] = None
    latency_ms: Optional[float] = None
    last_check: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemHealth:
    """Overall system health"""
    status: ComponentStatus
    components: List[ComponentHealth]
    timestamp: datetime = field(default_factory=datetime.now)
    version: str = "0.1.0"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat(),
            "version": self.version,
            "components": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "message": c.message,
                    "latency_ms": c.latency_ms,
                    "last_check": c.last_check.isoformat(),
                    "details": c.details
                }
                for c in self.components
            ]
        }


class HealthChecker:
    """System health checker
    
    Checks health of all system components and provides
    aggregated health status.
    """
    
    def __init__(self):
        self._checks: Dict[str, callable] = {}
        self._last_results: Dict[str, ComponentHealth] = {}
        
        # Register default checks
        self._register_default_checks()
    
    def _register_default_checks(self):
        """Register default health checks"""
        self.register_check("system", self._check_system)
        self.register_check("disk", self._check_disk)
        self.register_check("memory", self._check_memory)
    
    def register_check(self, name: str, check_fn: callable):
        """Register a health check function"""
        self._checks[name] = check_fn
    
    async def _check_system(self) -> ComponentHealth:
        """Check basic system health"""
        import sys
        return ComponentHealth(
            name="system",
            status=ComponentStatus.HEALTHY,
            message="System operational",
            details={
                "python_version": sys.version,
                "platform": sys.platform
            }
        )
    
    async def _check_disk(self) -> ComponentHealth:
        """Check disk space"""
        import shutil
        try:
            total, used, free = shutil.disk_usage("/")
            free_percent = (free / total) * 100
            
            if free_percent < 5:
                status = ComponentStatus.UNHEALTHY
                message = f"Critical: Only {free_percent:.1f}% disk space free"
            elif free_percent < 15:
                status = ComponentStatus.DEGRADED
                message = f"Warning: Only {free_percent:.1f}% disk space free"
            else:
                status = ComponentStatus.HEALTHY
                message = f"Disk space OK: {free_percent:.1f}% free"
            
            return ComponentHealth(
                name="disk",
                status=status,
                message=message,
                details={
                    "total_gb": round(total / (1024**3), 2),
                    "used_gb": round(used / (1024**3), 2),
                    "free_gb": round(free / (1024**3), 2),
                    "free_percent": round(free_percent, 2)
                }
            )
        except Exception as e:
            return ComponentHealth(
                name="disk",
                status=ComponentStatus.UNKNOWN,
                message=f"Failed to check disk: {e}"
            )
    
    async def _check_memory(self) -> ComponentHealth:
        """Check memory usage"""
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            memory_mb = usage.ru_maxrss / 1024  # Convert to MB on macOS
            
            return ComponentHealth(
                name="memory",
                status=ComponentStatus.HEALTHY,
                message=f"Memory usage: {memory_mb:.1f} MB",
                details={
                    "max_rss_mb": round(memory_mb, 2)
                }
            )
        except Exception as e:
            return ComponentHealth(
                name="memory",
                status=ComponentStatus.UNKNOWN,
                message=f"Failed to check memory: {e}"
            )
    
    async def check_component(self, name: str) -> ComponentHealth:
        """Check health of a specific component"""
        if name not in self._checks:
            return ComponentHealth(
                name=name,
                status=ComponentStatus.UNKNOWN,
                message=f"No health check registered for {name}"
            )
        
        import time
        start = time.time()
        try:
            result = await self._checks[name]()
            result.latency_ms = (time.time() - start) * 1000
            self._last_results[name] = result
            return result
        except Exception as e:
            logger.error(f"Health check failed for {name}: {e}")
            return ComponentHealth(
                name=name,
                status=ComponentStatus.UNHEALTHY,
                message=f"Health check failed: {e}",
                latency_ms=(time.time() - start) * 1000
            )
    
    async def check_all(self) -> SystemHealth:
        """Check health of all components"""
        components = []
        
        for name in self._checks:
            result = await self.check_component(name)
            components.append(result)
        
        # Determine overall status
        statuses = [c.status for c in components]
        if ComponentStatus.UNHEALTHY in statuses:
            overall = ComponentStatus.UNHEALTHY
        elif ComponentStatus.DEGRADED in statuses:
            overall = ComponentStatus.DEGRADED
        elif ComponentStatus.UNKNOWN in statuses:
            overall = ComponentStatus.DEGRADED
        else:
            overall = ComponentStatus.HEALTHY
        
        return SystemHealth(
            status=overall,
            components=components
        )
    
    def get_last_result(self, name: str) -> Optional[ComponentHealth]:
        """Get last health check result for a component"""
        return self._last_results.get(name)


# Global health checker instance
_health_checker: Optional[HealthChecker] = None


def get_health_checker() -> HealthChecker:
    """Get global health checker instance"""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker


async def check_health() -> SystemHealth:
    """Check system health"""
    return await get_health_checker().check_all()
