import streamlit as st
from ledger import FreeTierLedger
from router import TaskAwareRouter
from guardrails import validate_python_syntax

st.title("Free-Tier API Multi-Agent Orchestrator")

# Sidebar for User-Provided API Keys (Stored only in active session memory)
st.sidebar.subheader("API Key Configuration")
gemini_input = st.sidebar.text_input("Gemini API Key", type="password")
groq_input = st.sidebar.text_input("Groq API Key", type="password")
nvidia_input = st.sidebar.text_input("NVIDIA NIM API Key", type="password")

if "ledger" not in st.session_state:
    st.session_state.ledger = FreeTierLedger()
    st.session_state.router = TaskAwareRouter(st.session_state.ledger)

st.sidebar.subheader("Provider Quota Status")
for name, data in st.session_state.ledger.providers.items():
    st.sidebar.text(f"{name}: {data['requests_this_minute']}/{data['rpm_limit']} RPM")

task_type = st.selectbox("Select Task Type", ["massive_context", "speed_coding", "deep_reasoning"])
prompt = st.text_area("Enter your code modification instruction:")

if st.button("Execute Orchestration Task"):
    if not gemini_input and not groq_input:
        st.warning("Please enter at least one provider API key in the sidebar to execute tasks.")
    else:
        chosen_model = st.session_state.router.select_model_for_task(task_type)
        if not chosen_model:
            st.error("All free-tier providers are currently rate-limited. Please wait a moment.")
        else:
            st.success(f"Task routed successfully to: **{chosen_model}**")
            st.session_state.ledger.log_request(chosen_model)

            sample_code = "print('Supply chain update applied')"
            is_valid, msg = validate_python_syntax(sample_code)
            if is_valid:
                st.info(f"Guardrail Check Passed: {msg}")
            else:
                st.error(f"Syntax Error Detected: {msg}")
