# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability within CursedMC, please follow these steps:

1. **Do NOT** open a public GitHub issue
2. Send an email to the maintainers with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
3. Allow up to 48 hours for an initial response

## Security Measures

CursedMC implements the following security measures:

- **Password Security**: bcrypt hashing with automatic salt generation
- **Rate Limiting**: Protection against brute-force login attempts
- **Path Traversal Protection**: Strict validation for file operations
- **Command Sanitization**: Input validation for console commands
- **Session Security**: HTTPOnly cookies with SameSite protection

## Best Practices

When deploying CursedMC:

1. Always use a strong password (8+ characters, mixed case, numbers)
2. Keep your system and dependencies updated
3. Use Ngrok or Playit.gg for secure tunneling instead of direct port forwarding
4. Regularly backup your server data
5. Monitor server logs for suspicious activity
