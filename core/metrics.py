"""
Metrics Collector for Parser Maker

Provides centralized metrics collection for monitoring pipeline performance,
LLM usage, and system health.
"""

import time
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from contextlib import contextmanager
import threading


logger = logging.getLogger(__name__)


class MetricType(Enum):
    """Types of metrics"""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


@dataclass
class MetricValue:
    """Single metric value with metadata"""
    name: str
    value: float
    metric_type: MetricType
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class TimerMetric:
    """Timer metric for measuring durations"""
    name: str
    start_time: float
    labels: Dict[str, str] = field(default_factory=dict)
    
    def stop(self) -> float:
        """Stop timer and return duration in seconds"""
        return time.time() - self.start_time


class MetricsCollector:
    """Centralized metrics collector
    
    Thread-safe singleton for collecting and exporting metrics.
    
    Usage:
        metrics = MetricsCollector()
        metrics.increment("requests_total", labels={"endpoint": "/api"})
        
        with metrics.timer("request_duration", labels={"endpoint": "/api"}):
            # do work
            pass
    """
    
    _instance: Optional["MetricsCollector"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "MetricsCollector":
        """Singleton pattern"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._metrics: Dict[str, MetricValue] = {}
        self._histograms: Dict[str, List[float]] = {}
        self._metrics_lock = threading.Lock()
        self._initialized = True
        
        # Initialize default metrics
        self._init_default_metrics()
    
    def _init_default_metrics(self):
        """Initialize default metrics"""
        # Pipeline metrics
        self.set_gauge("pipeline_current_step", 0)
        self.set_gauge("pipeline_total_steps", 6)
        self.set_gauge("pipeline_confirmations", 0)
        
        # LLM metrics
        self._set_counter("llm_requests_total", 0)
        self._set_counter("llm_errors_total", 0)
        self._set_counter("llm_tokens_used", 0)
        
        # Droid metrics
        self._set_counter("droid_commands_total", 0)
        self._set_counter("droid_restarts_total", 0)
        
        # Git metrics
        self._set_counter("git_commits_total", 0)
        self._set_counter("git_push_total", 0)
    
    def _get_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        """Generate unique key for metric with labels"""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"
    
    def _set_counter(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        """Set counter value"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            self._metrics[key] = MetricValue(
                name=name,
                value=value,
                metric_type=MetricType.COUNTER,
                labels=labels or {}
            )
    
    def increment(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None):
        """Increment counter metric"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            if key in self._metrics:
                self._metrics[key].value += value
            else:
                self._metrics[key] = MetricValue(
                    name=name,
                    value=value,
                    metric_type=MetricType.COUNTER,
                    labels=labels or {}
                )
    
    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        """Set gauge metric"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            self._metrics[key] = MetricValue(
                name=name,
                value=value,
                metric_type=MetricType.GAUGE,
                labels=labels or {}
            )
    
    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        """Add observation to histogram"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            if key not in self._histograms:
                self._histograms[key] = []
            self._histograms[key].append(value)
    
    @contextmanager
    def timer(self, name: str, labels: Optional[Dict[str, str]] = None):
        """Context manager for timing operations"""
        start = time.time()
        try:
            yield
        finally:
            duration = time.time() - start
            self.observe(f"{name}_seconds", duration, labels)
            logger.debug(f"Timer {name}: {duration:.3f}s")
    
    def get_metric(self, name: str, labels: Optional[Dict[str, str]] = None) -> Optional[float]:
        """Get current metric value"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            if key in self._metrics:
                return self._metrics[key].value
            return None
    
    def get_histogram_stats(self, name: str, labels: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        """Get histogram statistics"""
        key = self._get_key(name, labels)
        with self._metrics_lock:
            if key not in self._histograms or not self._histograms[key]:
                return {}
            
            values = self._histograms[key]
            sorted_values = sorted(values)
            count = len(values)
            
            return {
                "count": count,
                "sum": sum(values),
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / count,
                "p50": sorted_values[int(count * 0.5)],
                "p90": sorted_values[int(count * 0.9)] if count >= 10 else sorted_values[-1],
                "p99": sorted_values[int(count * 0.99)] if count >= 100 else sorted_values[-1],
            }
    
    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics as dictionary"""
        with self._metrics_lock:
            result = {}
            
            # Add counters and gauges
            for key, metric in self._metrics.items():
                result[key] = {
                    "value": metric.value,
                    "type": metric.metric_type.value,
                    "labels": metric.labels,
                    "timestamp": metric.timestamp.isoformat()
                }
            
            # Add histogram stats
            for key in self._histograms:
                stats = self.get_histogram_stats(key.split("{")[0])
                if stats:
                    result[f"{key}_stats"] = stats
            
            return result
    
    def reset(self):
        """Reset all metrics"""
        with self._metrics_lock:
            self._metrics.clear()
            self._histograms.clear()
            self._init_default_metrics()
    
    def export_prometheus(self) -> str:
        """Export metrics in Prometheus format"""
        lines = []
        with self._metrics_lock:
            for key, metric in self._metrics.items():
                metric_name = metric.name.replace(".", "_")
                if metric.labels:
                    label_str = ",".join(f'{k}="{v}"' for k, v in metric.labels.items())
                    lines.append(f"{metric_name}{{{label_str}}} {metric.value}")
                else:
                    lines.append(f"{metric_name} {metric.value}")
        
        return "\n".join(lines)


# Convenience functions
_collector: Optional[MetricsCollector] = None


def get_metrics() -> MetricsCollector:
    """Get global metrics collector instance"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


def increment(name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None):
    """Increment counter metric"""
    get_metrics().increment(name, value, labels)


def set_gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None):
    """Set gauge metric"""
    get_metrics().set_gauge(name, value, labels)


def observe(name: str, value: float, labels: Optional[Dict[str, str]] = None):
    """Add observation to histogram"""
    get_metrics().observe(name, value, labels)


def timer(name: str, labels: Optional[Dict[str, str]] = None):
    """Context manager for timing operations"""
    return get_metrics().timer(name, labels)
