# Requirements Document

## Introduction

The Service Logs Dashboard is a redesigned `/services/logs/` page that provides a modern UI for monitoring and managing three systemd services: `daphne.service`, `camera-daemon.service`, and `celery.service`. The dashboard enables real-time log streaming via WebSocket, service lifecycle management (start/stop/restart), service status monitoring with visual indicators, and log filtering/search capabilities. Built on the existing Django + Django Channels stack with Bootstrap 5, SweetAlert2, and Material Design Icons.

## Glossary

- **Dashboard**: The `/services/logs/` page that displays service status cards, action controls, and a log viewer
- **Allowed_Services**: The fixed set of three systemd services: `daphne.service`, `camera-daemon.service`, `celery.service`
- **Service_Card**: A UI component displaying a single service's name, status indicator, uptime, memory usage, and PID
- **Log_Viewer**: A terminal-style UI component that displays real-time log output from a selected service
- **Status_Poller**: A client-side JavaScript timer that fetches service status every 5 seconds via HTTP
- **Log_Consumer**: The Django Channels WebSocket consumer that streams journalctl output to the browser
- **Service_Action**: One of the allowed systemctl operations: start, stop, restart, enable, or disable
- **Log_Level**: A classification of log line severity: error, warning, info, or debug
- **Status_Indicator**: A visual badge showing service state using color coding (green=active, red=failed, gray=inactive)

## Requirements

### Requirement 1: Service Restriction

**User Story:** As a system administrator, I want the dashboard to show only the three designated services, so that I can focus on the relevant services without distraction or risk of managing unrelated services.

#### Acceptance Criteria

1. THE Dashboard SHALL display exactly three services: `daphne.service`, `camera-daemon.service`, and `celery.service`
2. IF an HTTP request references a service not in Allowed_Services, THEN THE Dashboard SHALL reject the request with HTTP 400 status and a JSON response body containing a success field set to false and a message field indicating the service is not allowed
3. IF a WebSocket connection is attempted for a service not in Allowed_Services, THEN THE Log_Consumer SHALL reject the connection by closing it with WebSocket close code 4003 without sending any log data
4. THE Dashboard SHALL define Allowed_Services as the set containing exactly: `daphne.service`, `camera-daemon.service`, and `celery.service`

### Requirement 2: Service Status Monitoring

**User Story:** As a system administrator, I want to see the real-time status of each service, so that I can quickly identify which services are running, stopped, or failed.

#### Acceptance Criteria

1. WHEN the Dashboard page loads, THE Dashboard SHALL display exactly 3 Service_Cards (one for daphne.service, one for camera-daemon.service, one for celery.service), each showing its current status, display name, and icon
2. WHILE the Dashboard page is open, THE Status_Poller SHALL fetch updated status for all three services every 5 seconds
3. WHEN a status response is received, THE Dashboard SHALL update each Service_Card with the current status, uptime (formatted as human-readable duration, e.g. "2h 15m"), memory usage in megabytes rounded to 1 decimal place, and PID
4. THE Status_Poller SHALL return a response containing exactly 3 service entries with fields: name, status (one of "active", "inactive", "failed", "activating", or "unknown"), display_name, uptime, memory_mb, and pid
5. WHEN the status of a service is "active", THE Status_Indicator SHALL display a green badge with the text "Running"
6. WHEN the status of a service is "inactive", THE Status_Indicator SHALL display a gray badge with the text "Stopped"
7. WHEN the status of a service is "failed", THE Status_Indicator SHALL display a red badge with the text "Failed"
8. IF the status of a service is "activating" or "unknown", THEN THE Status_Indicator SHALL display a yellow badge with the text "Activating" or "Unknown" respectively
9. IF the Status_Poller request fails or does not respond within 10 seconds, THEN THE Dashboard SHALL retain the last known status for each Service_Card and display a visible connection error indicator until the next successful poll
10. WHILE the Dashboard is waiting for the first status response after page load, THE Dashboard SHALL display a loading indicator on each Service_Card until status data is received

### Requirement 3: Service Lifecycle Management

**User Story:** As a system administrator, I want to start, stop, and restart services from the dashboard, so that I can manage service lifecycles without using the terminal.

#### Acceptance Criteria

1. WHEN a user clicks a Service_Action button, THE Dashboard SHALL disable the clicked button, display a loading indicator, and send a POST request with the service name and action to the action endpoint within 10 seconds
2. WHEN a valid Service_Action request is received, THE Dashboard SHALL execute the corresponding systemctl command with a timeout of 30 seconds and return a JSON response containing a "success" boolean field and a "message" string field
3. IF a Service_Action request contains a service not in Allowed_Services, THEN THE Dashboard SHALL reject the request with HTTP 400 and a JSON response with success=false and a message indicating the service is not permitted
4. IF a Service_Action request contains an action not in the allowed set (start, stop, restart, enable, disable), THEN THE Dashboard SHALL reject the request with HTTP 400 and a JSON response with success=false and a message indicating the action is not permitted
5. IF the systemctl command fails with a non-zero exit code, THEN THE Dashboard SHALL return a JSON response with success=false and the error message truncated to 500 characters
6. WHEN a Service_Action completes successfully, THE Dashboard SHALL re-enable the action button, remove the loading indicator, and display a success toast notification using SweetAlert2 for 3 seconds
7. IF a Service_Action fails, THEN THE Dashboard SHALL re-enable the action button, remove the loading indicator, and display an error toast notification using SweetAlert2 for 5 seconds
8. IF a user is not authenticated, THEN THE Dashboard SHALL reject the Service_Action request and redirect to the login page
9. IF the systemctl command does not complete within 30 seconds, THEN THE Dashboard SHALL terminate the command and return a JSON response with success=false and a message indicating the operation timed out

### Requirement 4: Real-Time Log Streaming

**User Story:** As a system administrator, I want to view real-time logs from each service, so that I can monitor service behavior and troubleshoot issues as they occur.

#### Acceptance Criteria

1. WHEN a user selects a service for log viewing, THE Log_Consumer SHALL validate the service name against the allowed services list, establish a WebSocket connection, and begin streaming log lines starting with the most recent 50 lines from journalctl
2. WHEN a new log line is received from journalctl, THE Log_Consumer SHALL send it to the client as a JSON message containing the line text and a message type field set to "stream"
3. WHEN the WebSocket connection is closed, THE Log_Consumer SHALL terminate the journalctl subprocess within 5 seconds
4. WHEN a client sends a "get_history" command with a lines count parameter, THE Log_Consumer SHALL return the last N log lines (default 100, maximum 500) from journalctl with the message type field set to "history"
5. THE Log_Viewer SHALL maintain a buffer of at most 2000 log lines, removing the oldest lines when the buffer is full
6. WHILE log lines are streaming and the user has not manually scrolled upward, THE Log_Viewer SHALL auto-scroll to show the most recent log entry
7. IF the requested service name is not in the allowed services list, THEN THE Log_Consumer SHALL reject the WebSocket connection with close code 4003 and not spawn a journalctl subprocess
8. IF the journalctl subprocess exits unexpectedly or fails to start, THEN THE Log_Consumer SHALL send an error message to the client indicating the log stream has ended and close the WebSocket connection
9. IF the WebSocket connection is lost due to a network interruption, THEN THE Log_Viewer SHALL display a disconnected indicator and attempt to reconnect after 3 seconds with exponential backoff up to a maximum interval of 30 seconds

### Requirement 5: Log Filtering and Search

**User Story:** As a system administrator, I want to filter and search through logs, so that I can quickly find relevant log entries without reading through all output.

#### Acceptance Criteria

1. WHEN a user enters a search query of up to 200 characters, THE Log_Viewer SHALL display only log lines containing the query text (case-insensitive substring match)
2. WHEN a user selects a Log_Level filter, THE Log_Viewer SHALL display only log lines matching the selected level
3. WHEN both a search query and a Log_Level filter are active, THE Log_Viewer SHALL display only log lines matching both criteria
4. WHEN the search query is empty and the level filter is "all", THE Log_Viewer SHALL display all log lines in the buffer
5. THE Log_Viewer SHALL classify each log line using case-insensitive keyword matching in the following priority order: as "error" when it contains ERROR, CRITICAL, or FATAL; as "warning" when it contains WARNING or WARN; as "debug" when it contains DEBUG; and as "info" if none of the above keywords are found
6. WHILE a search query or Log_Level filter is active, THE Log_Viewer SHALL apply the active filter criteria to newly arriving log lines, displaying only those that match
7. IF the active filter criteria match zero log lines in the buffer, THEN THE Log_Viewer SHALL display an empty-state message indicating that no log lines match the current filter

### Requirement 6: Authentication and Security

**User Story:** As a system administrator, I want the dashboard to be accessible only to authenticated users, so that unauthorized users cannot view logs or manage services.

#### Acceptance Criteria

1. WHEN an unauthenticated user attempts to access any Dashboard endpoint, THE Dashboard SHALL return an HTTP 302 redirect to the login page
2. WHEN an unauthenticated user attempts a WebSocket connection, THE Log_Consumer SHALL reject the connection by closing the socket with close code 4001 without sending any log data
3. THE Dashboard SHALL validate CSRF tokens on all POST requests
4. IF a POST request is received with an invalid or missing CSRF token, THEN THE Dashboard SHALL reject the request with HTTP 403 and SHALL NOT execute the requested action
5. THE Dashboard SHALL use list-based subprocess calls without shell=True to prevent command injection
6. THE Log_Viewer SHALL HTML-escape all log line content by replacing the characters <, >, &, ", and ' with their corresponding HTML entities before inserting it into the DOM to prevent XSS

### Requirement 7: Error Handling and Resilience

**User Story:** As a system administrator, I want the dashboard to handle errors gracefully, so that I can understand what went wrong and recover without refreshing the page.

#### Acceptance Criteria

1. IF the WebSocket connection is lost, THEN THE Log_Viewer SHALL display a "Disconnected" indicator and attempt reconnection starting after 3 seconds, doubling the delay on each subsequent attempt up to a maximum interval of 30 seconds
2. IF the journalctl subprocess exits while the WebSocket connection is still open, THEN THE Log_Consumer SHALL send a JSON message containing an error field indicating the log stream has ended, and close the WebSocket connection with close code 1011
3. IF the systemctl status command returns a non-zero exit code or produces no parseable output, THEN THE Status_Poller SHALL return "unknown" as the status for that service
4. IF the systemctl status command does not complete within 5 seconds, THEN THE Status_Poller SHALL terminate the command and return "unknown" as the status for that service
5. IF a status fetch fails for one or more services, THEN THE Status_Poller SHALL continue polling on the next 5-second interval without interrupting status updates for the remaining services
6. WHILE the Log_Viewer is attempting reconnection, THE Log_Viewer SHALL display a "Reconnecting..." indicator showing the connection is not yet restored

### Requirement 8: User Interface Design

**User Story:** As a system administrator, I want a bold modern UI that is easy to view and navigate, so that I can efficiently monitor and manage services.

#### Acceptance Criteria

1. THE Dashboard SHALL use Bootstrap 5 for responsive layout and styling
2. THE Dashboard SHALL use Material Design Icons for service and action icons
3. WHILE the viewport width is 768px or greater, THE Dashboard SHALL display three Service_Cards in a single horizontal row of equal-width columns
4. WHILE the viewport width is less than 768px, THE Dashboard SHALL stack Service_Cards vertically in a single column
5. THE Log_Viewer SHALL use a monospace font with light-colored text on a dark background
6. THE Dashboard SHALL provide breadcrumb navigation showing the current page location within the site hierarchy
7. THE Dashboard SHALL display a color-coded status indicator on each Service_Card representing the service state (active, inactive, or failed)
8. THE Dashboard SHALL display action buttons (Start, Stop, Restart) for each service, visible without scrolling on desktop viewports
