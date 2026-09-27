# Presencialidad 5.3.0 — Notas de versión

## Enfoque

Presencialidad 5.3.0 cierra la rama local-first con funciones docentes, identidad institucional, internacionalización ampliada e integración opcional con Google/Gmail.

## Cambios principales

- Instalación nueva sin usuarios, aulas, alumnos ni registros precargados.
- `John Doe` y `Jane Doe` se usan únicamente como placeholders visuales.
- Asistente inicial para institución y primera cuenta Administrador.
- Perfil editable: nombre, correo, idioma y avatar.
- Administración de sesiones y cierre de otras sesiones.
- Integración opcional con Google mediante OAuth 2.0 + PKCE.
- Permiso de Gmail separado y limitado a `gmail.send` para avisos familiares.
- Backups manuales y automáticos de SQLite, restauración con copia preventiva.
- Datos locales fuera de la carpeta de instalación; modo portable opcional.
- Archivado/restauración de aulas y baja/restauración de alumnos.
- Protección del último administrador activo.
- Bloqueo de inicio de sesión tras intentos fallidos repetidos.
- CSRF, cookies HttpOnly/SameSite y cabeceras de seguridad.
- Migración de una base anterior mediante `--import-db`.
- Scripts de construcción para PyInstaller e Inno Setup.
- Dockerfile y docker-compose para despliegue de servidor único.
- Sección independiente **Notas privadas** para el docente, con búsqueda, filtro por aula, edición y eliminación; cada nota pertenece exclusivamente a su autor.
- Cuenta Google opcional mediante OAuth 2.0 + PKCE y prueba real de envío con Gmail usando `gmail.send`.
- Traducciones funcionales a **Русский** y **العربية**, además de los cinco idiomas existentes.
- Interfaz RTL automática al seleccionar árabe.
- Nombre de la institución visible en la pantalla previa al inicio de sesión y actualizable desde Administración.

## Compatibilidad

La base SQLite se migra al iniciar. Antes de migrar una instalación real se recomienda generar una copia de seguridad de la versión anterior.

## Nota sobre Google

La aplicación no incluye un Client ID ni secretos de Google. La institución debe registrar su propio proyecto OAuth y configurarlo desde Administración o mediante variables de entorno.

## Nota sobre despliegue

SQLite está pensado para una instancia de servidor. Para escalar a varias instancias concurrentes debe sustituirse por una base de datos de servidor, por ejemplo PostgreSQL.
