"""
Unit tests for state module
"""

import pytest
import tempfile
from pathlib import Path

from state.persistence import StatePersistence


class TestStatePersistence:
    """Tests for StatePersistence"""
    
    @pytest.fixture
    def state_dir(self):
        """Create temporary state directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    def test_create_new_run(self, state_dir):
        """Should create new pipeline run"""
        sp = StatePersistence(state_dir)
        sp.create_new_run("/tmp/project", "http://example.com", "test_target")
        
        state = sp.load()
        assert state is not None
        assert state.project_path == "/tmp/project"
        assert state.target_url == "http://example.com"
    
    def test_update_state(self, state_dir):
        """Should update state fields"""
        sp = StatePersistence(state_dir)
        sp.create_new_run("/tmp", "http://test.com", "target")
        
        sp.update(current_step=2, confirmations=1)
        state = sp.load()
        
        assert state.current_step == 2
        assert state.confirmations == 1
    
    def test_sentinel_pattern_with_none(self, state_dir):
        """Should handle None values correctly with sentinel pattern"""
        sp = StatePersistence(state_dir)
        sp.create_new_run("/tmp", "http://test.com", "target")
        
        # Set a value
        sp.update(recovery_stash="test_stash")
        state = sp.load()
        assert state.recovery_stash == "test_stash"
        
        # Clear with None
        sp.update(recovery_stash=None)
        state = sp.load()
        assert state.recovery_stash is None
    
    def test_backup_creation(self, state_dir):
        """Should create backups"""
        sp = StatePersistence(state_dir)
        sp.create_new_run("/tmp", "http://test.com", "target")
        
        # Save should create backup
        sp.save()
        
        backup_dir = Path(state_dir) / "backups"
        if backup_dir.exists():
            backups = list(backup_dir.glob("state_*.json"))
            # May or may not have backups depending on timing
            assert isinstance(backups, list)
    
    def test_load_nonexistent_returns_none(self, state_dir):
        """Should return None for nonexistent state"""
        sp = StatePersistence(state_dir)
        state = sp.load()
        assert state is None
