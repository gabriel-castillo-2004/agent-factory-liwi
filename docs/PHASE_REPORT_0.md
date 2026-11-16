# PHASE_REPORT_0 — Bootstrap

## 1. Archivos creados / modificados
- `.gitignore`: Reglas para ignorar `.venv/`, caches de Python y pytest/ruff, `.coverage`, `htmlcov/` y `.env`.
- `.env.example`: Plantilla de variables de entorno sin valores ni secretos (INV-07).
- `pyproject.toml`: Configuración del proyecto (Python >=3.11), dependencias fijas de desarrollo (`pytest==9.2.0`, `pytest-cov==7.1.0`, `pytest-socket==0.8.1`, `pytest-timeout==2.4.0`, `ruff==0.17.5`), configuración de `pytest` (`timeout = 30`, `timeout_method = "thread"`, `addopts = "--disable-socket"`), linter `ruff` y cobertura de código.
- `config/limits.toml`: Límites presupuestarios duros por estado (`INTERVIEW=6`, `DEBATE=3`, `MEDIATION=2`, `BUILDING=15`, `ci_rounds=3`) según INV-15 e INV-17.
- `config/models.toml`: Mapeo de estados a `"CONFIGURE_ME"` sin nombres reales de modelos codificados (INV-12, sección 2 y 4).
- `.github/workflows/ci.yml`: Pipeline de CI con permisos de solo lectura (`permissions: contents: read`), tope de 10 minutos (`timeout-minutes: 10`), linter `ruff` y `pytest` con cobertura.
- `docs/decisions/ADR-000-pytest-timeout.md`: Justificación técnica de `pytest-timeout` como única dependencia adicional a la lista inicial del MASTER_PROMPT bajo INV-06 e INV-08.
- Estructura de paquetes en `src/agent_factory/` (sin lógica de negocio):
  - `src/agent_factory/__init__.py`
  - `src/agent_factory/domain/__init__.py`
  - `src/agent_factory/ports/__init__.py`
  - `src/agent_factory/adapters/__init__.py`
  - `src/agent_factory/agents/__init__.py`
- `templates/.gitkeep`: Directorio reservado para plantillas base de aplicaciones (Fase 2).
- `tests/test_bootstrap.py`: Pruebas iniciales de validación de bootstrap:
  - `test_trivial_pass`: Verificación trivial de descubrimiento de pruebas y entorno.
  - `test_network_socket_blocked`: Verificación de bloqueo de sockets de red (INV-03).
  - `test_pytest_timeout_configured_in_pyproject`: Verificación de configuración de `timeout` (30s) y `timeout_method` (`thread`) en `pyproject.toml`.

## 2. Resultado de la suite y cobertura
- **Comandos ejecutados:**
  - `ruff check .` -> Pasó sin errores (`All checks passed!`).
  - `pytest --cov=src/agent_factory` -> Pasó al 100%.
- **Resultado:**
  - Total pruebas: 3 pasadas, 0 falladas, 0 omitidas.
  - Cobertura: 100% en `src/agent_factory` (paquetes iniciales).
  - Tiempo de ejecución: 0.09s.
  - Red bloqueada activa (`--disable-socket`) y timeout activo (`30.0s`, `thread`).

## 3. Criterios cumplidos o no
- [x] Rama `phase-0` creada y en uso exclusivo.
- [x] `.gitignore` creado y `.venv` verificado como ignorado sin rastro en `git status`.
- [x] `pyproject.toml` con Python >=3.11 y versiones exactas fijadas en `.venv`.
- [x] `docs/decisions/ADR-000-pytest-timeout.md` documentado justificando `pytest-timeout` bajo INV-08 e INV-06.
- [x] Estructura de directorios de la sección 6 con subpaquetes y `__init__.py` sin lógica de negocio.
- [x] `config/limits.toml` con las claves requeridas.
- [x] `config/models.toml` con una clave por estado y valor `"CONFIGURE_ME"`.
- [x] `.env.example` sin valores y sin secretos.
- [x] `.github/workflows/ci.yml` configurado con `timeout-minutes: 10` y `permissions: contents: read`.
- [x] `tests/test_bootstrap.py` con las tres pruebas requeridas pasando.
- [x] Criterio de Fase 0 del MASTER_PROMPT: CI verde con prueba trivial y red bloqueada en la suite.

## 4. Decisiones tomadas y supuestos
- **ADR-000**: Uso de `pytest-timeout==2.4.0` con `timeout_method = "thread"` en `pyproject.toml` para evitar tests colgados en Windows y garantizar el cumplimiento de INV-06.
- **Topes y red**: `addopts = "--disable-socket"` habilitado globalmente en `pyproject.toml` para garantizar la invariante INV-03 desde la configuración raíz del runner.
- **Sin secretos**: `.env.example` solo expone variables sin ningún valor de ejemplo para respetar INV-07.

## 5. Riesgos o deudas detectadas
- Ningún riesgo bloqueante detectado en Fase 0.
- En la Fase 1 se implementarán las entidades de dominio, estados, puertos y adaptadores sin dependencias externas ni de red.
