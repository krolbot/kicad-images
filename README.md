# KiCad container images

Отдельный репозиторий сборки образов для KiCad-сервисов лаборатории.

Публикуемые образы:

- `ghcr.io/krolbot/kicad-mcp:10.0.6-0.20.1-fr2.1.0-tools1`
- `ghcr.io/krolbot/kicad-gui:10.0.3-tools3`

Сборка запускается при изменении соответствующего Dockerfile или вручную через GitHub Actions. Compose-конфигурация и рабочие данные остаются в `WEBzaytsev/lab`; этот репозиторий содержит только исходники образов и их CI.
