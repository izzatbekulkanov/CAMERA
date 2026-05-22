# Implementation Plan: Service Logs Dashboard

## Overview

Redesign the `/services/logs/` page into a modern dashboard for monitoring and managing three systemd services (daphne, camera-daemon, celery). The implementation enhances the existing backend (views, WebSocket consumer) and replaces the frontend template with a Bootstrap 5 dashboard featuring service status cards, action controls, and a real-time log viewer with filtering.

## Tasks

- [ ] 1. Backend: Service status utilities and status endpoint
  - [x] 1.1 Create service status utility module with `get_service_status()` and `get_all_services_status()`
    - Create `attendance/services.py` with `ALLOWED_SERVICES`, `ALLOWED_ACTIONS`, `SERVICE_META` constants
    - Implement `ServiceStatus` dataclass with fields: name, display_name, status, sub_state, pid, uptime, memory_mb, cpu_percent, description, icon, color
    - Implement `get_service_status(service_name)` that calls `systemctl show` with a 5-second timeout and parses properties (ActiveState, SubState, MainPID, ActiveEnterTimestamp, MemoryCurrent, Description)
    - Implement `calculate_uptime(timestamp_str)` to convert ActiveEnterTimestamp to human-readable format (e.g., "2h 15m", "3d 4h")
    - Implement `get_all_services_status()` that returns status for all 3 allowed services, never raising exceptions (returns "unknown" on failure)
    - _Requirements: 1.1, 1.2, 1.4, 2.4, 7.3, 7.4_

  - [ ]* 1.2 Write property tests for service status utilities
    - **Property 3: Status Response Structure Invariant** — `get_all_services_status()` always returns exactly 3 entries with valid status values
    - **Property 11: Status Fallback on Failure** — when systemctl fails, status is "unknown" rather than an exception
    - **Validates: Requirements 1.1, 2.4, 7.3, 7.4**

  - [ ] 1.3 Implement `service_status_view` endpoint
    - Add `service_status_view(request)` to `attendance/views.py` (or a new views module) that returns JSON with all 3 service statuses
    - Require `@login_required` decorator
    - Return JSON structure: `{"services": [{name, status, display_name, uptime, memory_mb, pid, icon, color}, ...]}`
    - _Requirements: 2.1, 2.4, 6.1_

  - [ ]* 1.4 Write unit tests for `service_status_view`
    - Test that response contains exactly 3 services
    - Test that unauthenticated requests redirect to login
    - Test response JSON structure
    - _Requirements: 2.4, 6.1_

- [ ] 2. Backend: Enhance service action view with validation
  - [ ] 2.1 Refactor `service_action_view` to enforce ALLOWED_SERVICES whitelist
    - Modify existing `service_action_view` in `attendance/views.py` to validate service name against `ALLOWED_SERVICES` from `attendance/services.py`
    - Return HTTP 400 with `{success: false, message: "..."}` if service not in whitelist
    - Return HTTP 400 with `{success: false, message: "..."}` if action not in ALLOWED_ACTIONS
    - Add 30-second timeout to subprocess call
    - Truncate error messages to 500 characters
    - _Requirements: 1.2, 3.2, 3.3, 3.4, 3.5, 3.9, 6.3, 6.5_

  - [ ]* 2.2 Write property tests for service action validation
    - **Property 1: Service Restriction Enforcement** — any service not in allowed set is rejected with HTTP 400
    - **Property 2: Action Validation** — any action not in allowed set is rejected with HTTP 400
    - **Property 9: Error Message Truncation** — error messages never exceed 500 characters
    - **Validates: Requirements 1.2, 3.3, 3.4, 3.5**

- [ ] 3. Backend: Enhanced WebSocket consumer
  - [ ] 3.1 Rewrite `ServiceLogConsumer` with service validation and history support
    - Update `attendance/consumers.py` `ServiceLogConsumer` class
    - Add `ALLOWED_SERVICES` set validation in `connect()` — reject with close code 4003 if service not allowed
    - Add `receive()` method to handle `get_history` command with line count (default 100, max 500)
    - Add `send_history(lines_count)` method using `journalctl -u service -n N --no-pager`
    - Modify `stream_logs()` to start with last 50 lines (`-n 50`) and send JSON with `type` field ("stream" or "history")
    - Ensure proper subprocess cleanup in `disconnect()` with process termination
    - Send error JSON and close connection if journalctl subprocess exits unexpectedly
    - _Requirements: 1.3, 4.1, 4.2, 4.3, 4.4, 4.7, 4.8, 6.2_

  - [ ]* 3.2 Write property tests for WebSocket consumer
    - **Property 1: Service Restriction Enforcement** — WebSocket rejects non-allowed services with code 4003
    - **Property 7: History Line Count Cap** — get_history returns at most min(N, 500) lines
    - **Validates: Requirements 1.3, 4.4, 4.7**

- [ ] 4. Checkpoint - Ensure backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. URL routing setup
  - [ ] 5.1 Add `service_status_view` URL route
    - Add `path("services/status/", service_status_view, name="service_status")` to `attendance/urls.py`
    - Import the new view function
    - Verify WebSocket routing in `attendance/routing.py` already handles `ws/logs/<service_name>/`
    - _Requirements: 2.2, 2.4_

- [ ] 6. Frontend: Dashboard template
  - [ ] 6.1 Create the service logs dashboard template
    - Replace `templates/pages/service_logs.html` with a modern Bootstrap 5 dashboard layout
    - Include breadcrumb navigation (Bosh sahifa > Servis Loglari)
    - Create 3 Service Status Cards in a responsive row (col-md-4): each card shows service display_name, MDI icon, status badge (green/red/gray/yellow), uptime, memory, PID
    - Add action buttons (Start, Stop, Restart) per service card with MDI icons
    - Create full-width terminal-style log viewer section with dark background, monospace font, auto-scroll
    - Add service selector tabs/buttons above log viewer to switch between services
    - Add search input (max 200 chars) and log level filter dropdown (All, Error, Warning, Info, Debug)
    - Responsive: 3 cards horizontal on md+, stacked on mobile
    - _Requirements: 1.1, 2.1, 2.3, 2.5, 2.6, 2.7, 2.8, 5.1, 5.2, 5.4, 5.7, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [ ] 6.2 Implement JavaScript: status polling and service actions
    - Implement `pollStatus()` function that fetches `/services/status/` every 5 seconds and updates card UI
    - Implement `performAction(service, action)` that sends POST to `/services/action/` with CSRF token
    - Show loading state on action buttons during request, re-enable on completion
    - Display SweetAlert2 success toast (3s) on success, error toast (5s) on failure
    - Handle poll failure: retain last known status, show connection error indicator
    - Show loading indicators on cards until first status response
    - _Requirements: 2.2, 2.9, 2.10, 3.1, 3.6, 3.7, 3.8, 7.5_

  - [ ] 6.3 Implement JavaScript: WebSocket log streaming with reconnection
    - Implement WebSocket connection to `ws/logs/{service_name}/`
    - Handle incoming messages: append log lines to viewer, maintain 2000-line buffer (remove oldest)
    - Implement auto-scroll behavior (scroll to bottom unless user has scrolled up)
    - Implement reconnection with exponential backoff (3s initial, doubling, max 30s)
    - Show "Disconnected" / "Reconnecting..." indicators on connection loss
    - Send `get_history` command on initial connection for historical context
    - HTML-escape all log content before DOM insertion (replace <, >, &, ", ')
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 4.9, 6.6, 7.1, 7.6, 8.5_

  - [ ] 6.4 Implement JavaScript: client-side log filtering and search
    - Implement `filterLogs(query, level)` that shows/hides log lines based on search text and level
    - Implement `detectLogLevel(line)` — classify as error (ERROR/CRITICAL/FATAL), warning (WARNING/WARN), debug (DEBUG), or info (default)
    - Apply filter to existing buffer and newly arriving lines
    - Show empty-state message when no lines match filter
    - Color-code log lines by detected level (red for error, yellow for warning, etc.)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [ ]* 6.5 Write property tests for log filtering logic (Python test of equivalent logic)
    - **Property 4: Log Filter Correctness** — filtered result equals intersection of search match and level match
    - **Property 5: Log Filter Idempotency** — filter(filter(logs, q, level), q, level) == filter(logs, q, level)
    - **Property 6: Log Level Classification Determinism** — same input always returns same level
    - **Property 8: Log Buffer Size Invariant** — buffer never exceeds 2000 entries
    - **Property 10: XSS Prevention via Escaping** — log lines with HTML chars are escaped in output
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.5, 4.5, 6.6**

- [ ] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Integration and wiring
  - [ ] 8.1 Wire all components together and verify end-to-end flow
    - Ensure `service_logs_view` uses `get_all_services_status()` from `attendance/services.py` to pass initial service data to template
    - Verify template loads with correct context variables (services list, breadcrumbs)
    - Verify WebSocket routing connects to enhanced `ServiceLogConsumer`
    - Verify status polling endpoint is accessible and returns correct JSON
    - Verify action endpoint validates against ALLOWED_SERVICES
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 3.2, 4.1_

  - [ ]* 8.2 Write integration tests for the full dashboard flow
    - Test page load returns 200 with 3 service cards in context
    - Test status endpoint returns valid JSON with 3 services
    - Test action endpoint rejects disallowed services/actions
    - Test unauthenticated access redirects to login
    - _Requirements: 1.1, 1.2, 2.4, 3.3, 3.4, 6.1_

- [ ] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The existing `service_logs_view` and `service_action_view` in `attendance/views.py` will be refactored in place
- The existing `ServiceLogConsumer` in `attendance/consumers.py` will be enhanced with validation and history support
- No new dependencies are required — all functionality uses the existing Django Channels, Bootstrap 5, SweetAlert2, and MDI stack

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1", "3.1"] },
    { "id": 2, "tasks": ["1.4", "2.2", "3.2", "5.1"] },
    { "id": 3, "tasks": ["6.1"] },
    { "id": 4, "tasks": ["6.2", "6.3", "6.4"] },
    { "id": 5, "tasks": ["6.5", "8.1"] },
    { "id": 6, "tasks": ["8.2"] }
  ]
}
```
