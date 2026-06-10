You are an expert cloud-native software engineer and AI architect specializing in LangGraph (specifically multi-server/remote graphs), A2A (Agent-to-Agent), restate and Google's Gemma 4-31B model routed through LiteLLM, and zero-trust service mesh networking with Istio on Kubernetes.


Your task is to write the complete Python code, Dockerfiles, and a Kubernetes Helm chart configuration for a distributed, microservice-based Agent-to-Agent (A2A) system using LangGraph to process German Child Benefit Applications ("Kindergeldantrag").



### Architectural Constraints

1. **Distributed Deployments:** Agent 1 and Agent 2 must be completely independent applications with their own codebases, their own Docker images, and separate Kubernetes Deployments. 

   - **Agent 1** acts as the primary API ingress and orchestrator.

   - **Agent 2** acts as an isolated microservice exposed only within the cluster mesh.

   - Communication between Agent 1 and Agent 2 must use LangGraph's remote graph clients (`RemoteGraph` or HTTP-based cross-graph execution).

2. **Inference Routing via LiteLLM:** Both services must connect to the `gemma-4-31b` model through a locally hosted **LiteLLM** proxy instance running inside the cluster. The Python code must initialize the LLM using the standard OpenAI/LiteLLM client interface, fetching the endpoint and credentials via environment variables (`LITELLM_API_BASE` and `LITELLM_API_KEY`).



### Use Case Scenario

1. **The User (Citizen):** Submits the application data (unstructured text, missing details, or XML) via an API client to Agent 1.

2. **Agent 1 (Der Sachbearbeiter / Case Worker Service):** 

   - Runs in Deployment A.

   - Validates structural completeness (checks for Tax IDs, birth certificates, IBAN).

   - If information is missing, it initiates a loop back to the user asking for clarification.

   - If complete, it calls Agent 2 over the network, passing the current state schema.

3. **Agent 2 (Der Abteilungsleiter / Department Head Service):**

   - Runs in Deployment B (Strictly isolated from external human contact).

   - Receives the payload via a secure internal endpoint.

   - Runs validation rules (automated fraud detection, check if payout exceeds €1,000 threshold requiring human oversight).

   - Makes the final "Bewilligt" (Approved) or "Abgelehnt" (Rejected) decision and returns the signed state to Agent 1.



### Deployment & Networking (Istio & Kubernetes)

The entire multi-image application must be packageable using a single Helm chart with distinct deployment directories:

1. **Agent 1 Deployment:** Publicly accessible via Istio Gateway and VirtualService.

2. **Agent 2 Deployment:** Pods must be net-isolated. An Istio `AuthorizationPolicy` must allow ingress traffic to Agent 2 **only** if the client principal is Agent 1's service account.

3. **LiteLLM Integration:** The Helm chart should assume LiteLLM is accessible at a cluster-local URL (e.g., `http://litellm-service.litellm.svc.cluster.local:4000`). Pass this URL into both deployments via environment variables.

4. **Dockerfiles:** Provide two distinct, optimized multi-stage Dockerfiles (`Dockerfile.agent1` and `Dockerfile.agent2`).



### Expected Output Structure

Please provide the complete, production-ready code split into the following file structure:



#### 1. Codebase: Agent 1 (Sachbearbeiter)

- `agent1/state.py`: Shared Pydantic schemas and state keys.

- `agent1/app.py`: Logic for validation tools and the primary graph that calls Agent 2 remotely. Uses the LiteLLM endpoint configuration for Gemma completions.

- `agent1/Dockerfile.agent1`: Production Dockerfile for Agent 1.



#### 2. Codebase: Agent 2 (Abteilungsleiter)

- `agent2/app.py`: High-privilege decision-making graph exposed via an internal FastAPI wrapper or LangGraph API server. Uses the LiteLLM endpoint configuration for Gemma completions.

- `agent2/Dockerfile.agent2`: Production Dockerfile for Agent 2.


#### 3. Helm Chart Configuration (`./charts/kindergeld-distributed-a2a/`)

- `Chart.yaml`: Standard Helm metadata.

- `values.yaml`: Configurable parameters for both images (tags, replicas, resource limits, Istio host configs, and the cluster-local `LITELLM_API_BASE` endpoint).

- `templates/deployment-agent1.yaml` & `templates/deployment-agent2.yaml`: Separate deployments with distinct Kubernetes `ServiceAccounts`, standard environment variables for LiteLLM routing, and `sidecar.istio.io/inject: "true"` annotations.

- `templates/services.yaml`: Kubernetes Services exposing port structures for both agents.

- `templates/istio-gateway.yaml` & `templates/istio-virtualservice.yaml`: Border routing to Agent 1 only.

- `templates/istio-auth-policy.yaml`: Istio `AuthorizationPolicy` securing Agent 2, ensuring only Agent 1's sidecar can talk to it.



All code and comments must be in German (or heavily commented in German), explaining how the state handoff over the network functions securely between the two containers.