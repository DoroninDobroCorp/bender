"""
Unit tests for pipeline module
"""

import pytest
import tempfile
import os
from pathlib import Path

from pipeline.step import Step, load_steps, StepValidationError
from pipeline.git_manager import GitManager


class TestStep:
    """Tests for Step class"""
    
    def test_step_creation(self):
        """Should create step with required fields"""
        step = Step(
            id=1,
            name="Test Step",
            prompt_template="Do something",
            completion_criteria=["criterion 1"]
        )
        assert step.id == 1
        assert step.name == "Test Step"
        assert step.prompt_template == "Do something"
        assert len(step.completion_criteria) == 1


class TestLoadSteps:
    """Tests for load_steps function"""
    
    def test_load_valid_yaml(self):
        """Should load valid YAML file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
steps:
  - id: 1
    name: "Test Step"
    prompt_template: "Do something"
    completion_criteria:
      - "criterion 1"
""")
            temp_path = f.name
        
        try:
            step_config = load_steps(temp_path)
            assert step_config.total_steps == 1
            assert step_config.steps[0].id == 1
            assert step_config.steps[0].name == "Test Step"
        finally:
            os.unlink(temp_path)
    
    def test_load_invalid_yaml_raises(self):
        """Should raise StepValidationError for invalid YAML"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("invalid: yaml: [")
            temp_path = f.name
        
        try:
            with pytest.raises(StepValidationError) as exc_info:
                load_steps(temp_path)
            assert "Invalid YAML" in str(exc_info.value)
        finally:
            os.unlink(temp_path)
    
    def test_load_missing_file_raises(self):
        """Should raise error for missing file"""
        with pytest.raises((FileNotFoundError, StepValidationError)):
            load_steps("/nonexistent/path.yaml")


class TestGitManager:
    """Tests for GitManager"""
    
    def test_git_manager_dry_run(self):
        """Should create GitManager in dry run mode"""
        gm = GitManager("/tmp", dry_run=True)
        assert gm.dry_run is True
        assert gm.project_path == Path("/tmp")
