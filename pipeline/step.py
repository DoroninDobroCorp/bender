"""
Step Definition - структура и загрузка шагов pipeline
"""

import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


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


@dataclass
class StepConfig:
    """Конфигурация всех шагов"""
    steps: List[Step]
    
    @classmethod
    def from_yaml(cls, yaml_path: str) -> "StepConfig":
        """Загрузить из YAML файла"""
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Steps config not found: {yaml_path}")
        
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        steps = []
        for step_data in data.get('steps', []):
            step = Step(
                id=step_data['id'],
                name=step_data['name'],
                prompt_template=step_data['prompt_template'],
                completion_criteria=step_data.get('completion_criteria', [])
            )
            steps.append(step)
        
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


def load_steps(yaml_path: str = None) -> StepConfig:
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
