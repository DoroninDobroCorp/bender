"""
Step Definition - структура и загрузка шагов pipeline
"""

import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field


class StepValidationError(Exception):
    """Error during step configuration validation"""
    pass


@dataclass
class Step:
    """Определение одного шага pipeline"""
    id: int
    name: str
    prompt_template: str
    completion_criteria: List[str] = field(default_factory=list)
    
    def get_prompt(self, **variables) -> str:
        """Получить промпт с подставленными переменными"""
        prompt = self.prompt_template
        for key, value in variables.items():
            prompt = prompt.replace(f"{{{key}}}", str(value))
        return prompt
    
    def validate(self) -> List[str]:
        """Validate step configuration
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        if self.id < 1:
            errors.append(f"Step ID must be positive, got {self.id}")
        
        if not self.name or not self.name.strip():
            errors.append(f"Step {self.id}: name is required")
        
        if not self.prompt_template or not self.prompt_template.strip():
            errors.append(f"Step {self.id}: prompt_template is required")
        
        return errors


@dataclass
class StepConfig:
    """Конфигурация всех шагов"""
    steps: List[Step]
    
    @classmethod
    def from_yaml(cls, yaml_path: str) -> "StepConfig":
        """Загрузить из YAML файла
        
        Raises:
            FileNotFoundError: If YAML file doesn't exist
            StepValidationError: If configuration is invalid
        """
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Steps config not found: {yaml_path}")
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise StepValidationError(f"Invalid YAML in {yaml_path}: {e}")
        
        if data is None:
            raise StepValidationError(f"Empty YAML file: {yaml_path}")
        
        if 'steps' not in data:
            raise StepValidationError(f"Missing 'steps' key in {yaml_path}")
        
        if not isinstance(data['steps'], list):
            raise StepValidationError(f"'steps' must be a list in {yaml_path}")
        
        if len(data['steps']) == 0:
            raise StepValidationError(f"No steps defined in {yaml_path}")
        
        steps = []
        all_errors = []
        seen_ids: Set[int] = set()
        
        for i, step_data in enumerate(data.get('steps', [])):
            # Check required fields
            if not isinstance(step_data, dict):
                all_errors.append(f"Step at index {i}: must be a dictionary")
                continue
            
            if 'id' not in step_data:
                all_errors.append(f"Step at index {i}: missing 'id' field")
                continue
            
            if 'name' not in step_data:
                all_errors.append(f"Step at index {i}: missing 'name' field")
                continue
                
            if 'prompt_template' not in step_data:
                all_errors.append(f"Step at index {i}: missing 'prompt_template' field")
                continue
            
            # Check for duplicate IDs
            step_id = step_data['id']
            if step_id in seen_ids:
                all_errors.append(f"Duplicate step ID: {step_id}")
            seen_ids.add(step_id)
            
            step = Step(
                id=step_data['id'],
                name=step_data['name'],
                prompt_template=step_data['prompt_template'],
                completion_criteria=step_data.get('completion_criteria', [])
            )
            
            # Validate individual step
            step_errors = step.validate()
            all_errors.extend(step_errors)
            
            steps.append(step)
        
        if all_errors:
            raise StepValidationError(
                f"Step configuration errors:\n" + "\n".join(f"  - {e}" for e in all_errors)
            )
        
        # Sort steps by ID
        steps.sort(key=lambda s: s.id)
        
        # Check for gaps in step IDs
        expected_ids = set(range(1, len(steps) + 1))
        actual_ids = {s.id for s in steps}
        if expected_ids != actual_ids:
            missing = expected_ids - actual_ids
            extra = actual_ids - expected_ids
            msg = []
            if missing:
                msg.append(f"Missing step IDs: {sorted(missing)}")
            if extra:
                msg.append(f"Unexpected step IDs: {sorted(extra)}")
            raise StepValidationError(f"Step ID sequence error: {'; '.join(msg)}")
        
        return cls(steps=steps)
    
    def get_step(self, step_id: int) -> Optional[Step]:
        """Получить шаг по ID"""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None
    
    @property
    def total_steps(self) -> int:
        """Общее количество шагов"""
        return len(self.steps)


def load_steps(yaml_path: Optional[str] = None) -> StepConfig:
    """Загрузить конфигурацию шагов
    
    Args:
        yaml_path: Путь к YAML файлу. Если None - используется default.
    
    Returns:
        StepConfig
    """
    if yaml_path is None:
        # Default path относительно этого файла
        yaml_path = Path(__file__).parent.parent / "steps" / "parser_steps.yaml"
    
    return StepConfig.from_yaml(str(yaml_path))
