from opsfleet_agent.entities.access import AccessScope


def describe_scope(scope: AccessScope) -> str:
    if scope.sees_all_brands:
        return "## Current user\nRole: CEO. Access: all brands."
    brands = ", ".join(sorted(scope.brands))
    return (
        f"## Current user\nRole: {scope.role}. Access limited to these brands: {brands}.\n"
        "Questions about any other brand cannot be answered from data: say it is outside "
        "the user's access without querying."
    )
