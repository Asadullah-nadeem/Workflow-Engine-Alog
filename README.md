# Universal Website Automation & Workflow Engine

A production-grade, modular, and resilient Python automation platform designed for authorized websites. Supports multi-URL orchestration, dynamic DOM form scanning, configurable authentication, interactive OTP/MFA handling, controlled concurrency, and real-time synchronization between a Glassmorphic Desktop GUI and Telegram.

---

## Key Capabilities

1. **Multi-URL Automation Manager**:

   - Add as many authorized URLs as required.
   - Independent status tracking per URL: Scan Status, Authentication Status, Automation Status, Last Execution, Result, and Execution History.
   - Batch controls: Scan Selected, Start Selected, Stop Selected, Start All, Stop All.
2. **Automatic DOM & Form Scanner**:

   - Real-time DOM inspection extracting  containers, input types (username, password, OTP/MFA, email, text), and interactive buttons.
   - Heuristic CSS selector discovery automatically populates optimal site selectors.
   - Discovers login triggers, logout anchors, and authenticated state indicators.
3. **Configurable Authentication & Interactive OTP Handling**:

   - Automated credential entry (username & securely stored password).
   - Pauses automation immediately upon detecting an OTP / 2FA requirement.
   - Prompts the user with an interactive Desktop GUI modal (🔐 OTP REQUIRED) and sends a safe Telegram operational notification.
   - Automatically resumes workflow upon user OTP submission.
   - **Zero Secret Leakage**: Plaintext passwords and OTPs are never logged to disk or sent to Telegram.
4. **Enterprise Chrome Automation Manager**:

   - High-level browser control: start(), connect(), open_url(),
     avigate(), ind_element(), wait_for_element(), xecute_action(), check_health(), stop(), cleanup(),
     estart().
   - Multi-tab isolation per site worker.
   - Automatic ProcessSingleton collision fallback if personal Chrome is active.
5. **Universal Workflow Engine & Scheduler**:

   - Controlled concurrency worker pool (semaphore-limited).
   - Start / Stop / Pause / Resume controls per website and in batch.
   - One-time or recurring execution intervals with max retries and timeout controls.
   - Continuous logout detection: detects session expiration and halts execution until re-authentication.
6. **Real-time Glassmorphic GUI Command Center**:

   - Native PyQt6 desktop window with embedded FastAPI WebEngine.
   - Dynamic Modals: Add Website, DOM Scan Report, Site Configuration, Interactive OTP Dialog, and Execution History.
   - Live streaming system terminal with WebSocket updates.
7. **Operational Telegram Notifications**:

   - Dispatches formatted alerts for Automation Started, OTP Required, Session Expired, Automation Completed, and Safe Diagnostics.
