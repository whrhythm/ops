"""Select project behavior from owned persisted records, never from prompt wording."""
from fastapi import HTTPException

from .models import AIProjectBinding, CodeProjectDeletion, WorkspaceProject


def resolve_project(db, environment_id, project_id):
    project = db.get(WorkspaceProject, project_id)
    if not project or project.environment_id != environment_id:
        raise HTTPException(404, 'projects_not_found')
    binding = db.get(AIProjectBinding, project_id)
    if binding:
        if binding.environment_id != environment_id or binding.state != 'ready':
            raise HTTPException(404, 'Project is unavailable')
        return 'assistant', binding
    if db.get(CodeProjectDeletion, project_id):
        raise HTTPException(409, 'projects_deleting')
    return 'application', project
