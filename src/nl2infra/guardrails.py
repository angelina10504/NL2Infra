from contracts import RunState

def check_guardrails(state: RunState) -> str:
    """
    Returns an empty string if guardrails pass, 
    or a rejection reason if they fail.
    """
    prompt = state.user_prompt.lower()
    
    # 1. Reject off-topic
    infrastructure_keywords = ["server", "database", "cluster", "deploy", "kubernetes", "pod", "service", "namespace", "ingress", "aws", "gcp"]
    if not any(kw in prompt for kw in infrastructure_keywords):
        return "off_topic: Request does not seem related to infrastructure."
        
    # 2. Reject basic injection
    injection_keywords = ["ignore previous", "system prompt", "bypass", "you are now"]
    if any(kw in prompt for kw in injection_keywords):
        return "injection_attempt: Potentially malicious prompt."
        
    # 3. Role-based checks (example)
    if state.user_role == "junior_dev":
        if "production" in prompt or "prod" in prompt:
            return "unauthorized: junior_dev cannot deploy to production."
            
    return ""
