# Presencialidad 5.3.0

Aplicación local-first para asistencia y seguimiento escolar. La versión 5.3.0 está pensada como una base estable para uso local y para despliegues institucionales de una sola instancia.

## Estado inicial

La instalación nueva empieza completamente vacía: **no hay cuentas, aulas, alumnos, asistencias ni reportes precargados**. La primera apertura muestra un asistente para crear la institución y la primera cuenta administradora.

`John Doe` y `Jane Doe` aparecen únicamente como **placeholders visuales** en algunos formularios. No se insertan como usuarios ni como alumnos.

## Arranque local

### Windows con Python 3

Ejecutá:

`INICIAR_APP.bat`

Se abre el servidor local en:

`http://127.0.0.1:8765/`

Los datos se guardan por defecto en:

`%LOCALAPPDATA%\Presencialidad\`

Allí se crean:

- `presencialidad.db`
- `backups/`
- `logs/`
- `uploads/`

### Modo portable

Ejecutá:

`INICIAR_PORTABLE.bat`

La base y las copias se guardan junto a la aplicación dentro de `runtime/`.

### Linux / macOS

```bash
chmod +x INICIAR_APP.sh
./INICIAR_APP.sh
```

En Linux se usa normalmente `~/.local/share/Presencialidad`; en macOS, `~/Library/Application Support/Presencialidad`.

## Primera ejecución

El asistente solicita:

1. Institución.
2. Nombre de la primera cuenta administradora.
3. Correo institucional.
4. Ciclo lectivo.
5. Idioma.
6. Contraseña de al menos 10 caracteres.

No existe una contraseña predeterminada ni una cuenta maestra oculta.

El **nombre de la institución se muestra antes de iniciar sesión**. Se obtiene desde la configuración pública mínima del servidor, sin exponer datos privados. Puede modificarse luego desde Administración.

## Funciones principales

- Inicio con “Mis clases de hoy”.
- Aulas y usuarios por rol.
- Asistencia: Presente, Ausente, Tarde, Justificada y Retiro anticipado.
- Motivos, hora de llegada y hora de retiro.
- “Marcar todos presentes”.
- Autoguardado de borradores y recuperación local cuando el backend desaparece.
- Historial individual y general.
- Alertas simples por ausencias, tardanzas y porcentaje de asistencia.
- Sección independiente de **Notas privadas** para docentes, con búsqueda, filtro por aula, edición y eliminación. Las notas solo son visibles para su autor.
- Reportes institucionales con seguimiento y estados.
- Calificaciones básicas.
- Calendario docente ligero.
- Avisos estructurados a familias.
- Importación CSV/XLSX.
- Exportación CSV/PDF.
- Búsqueda global por alumno, legajo o DNI según permisos.
- Auditoría de modificaciones.
- Idiomas: Español, English, 日本語, 한국어, 中文（普通话）, Русский y العربية. El árabe activa disposición RTL.
- Tema claro/oscuro y nueve colores secundarios.
- Copias de seguridad manuales y automáticas.
- Restauración con copia previa de seguridad.
- Aulas archivadas en lugar de eliminación destructiva.
- Perfil editable, sesiones abiertas y cierre de sesiones remotas.
- Conexión opcional a Google y permiso Gmail separado. Incluye envío de correo de prueba desde la cuenta conectada.

## Roles

### Docente

Puede trabajar con sus aulas, pasar asistencia, cargar calificaciones cuando corresponda, crear notas privadas, reportes y avisos.

### Preceptor

Puede consultar las aulas asignadas, contactos autorizados, asistencia y seguimiento de reportes.

### Directivo

Puede administrar la institución, usuarios, aulas, datos y reportes.

### Administrador

Tiene acceso técnico y administrativo completo. La primera cuenta creada por el asistente tiene este rol.

## Perfil y sesiones

En `Cuenta > Perfil` cada usuario puede modificar:

- Nombre visible.
- Correo institucional.
- Idioma preferido.
- Avatar HTTPS.
- Foto de Google si existe una cuenta vinculada.

El rol y las aulas asignadas siguen dependiendo de Administración.

En `Cuenta > Seguridad` se pueden consultar las sesiones abiertas, cerrar sesiones individuales o cerrar todas salvo la actual.

## Google y Gmail

La integración es opcional y está deliberadamente separada en dos permisos.

### Conectar Google

La conexión básica pide únicamente:

- `openid`
- `email`
- `profile`

Esto sirve para asociar la Cuenta de Google, correo, nombre y foto. No concede acceso a Gmail.

### Habilitar Gmail

Si el usuario decide activarlo, la aplicación solicita además:

`https://www.googleapis.com/auth/gmail.send`

Presencialidad utiliza este permiso únicamente para enviar avisos familiares creados dentro de la aplicación. No solicita lectura de la bandeja de entrada.

Una vez habilitado, `Cuenta > Google` ofrece **Enviar correo de prueba**. El mensaje se envía a la misma dirección Google conectada para comprobar que OAuth y `gmail.send` funcionan antes de usar avisos familiares reales.

### Configuración local en Windows

1. Crear un proyecto en Google Cloud.
2. Configurar OAuth consent screen.
3. Crear credenciales OAuth para aplicación de escritorio.
4. Copiar el Client ID en `Administración > Configuración institucional`.
5. Si las credenciales incluyen Client Secret, puede cargarse también.
6. Guardar.
7. Cada usuario podrá usar `Cuenta > Google > Conectar Google`.

En Windows los tokens persistentes se protegen con DPAPI del usuario de Windows.

### Configuración en servidor Linux / Docker

Para guardar tokens fuera de Windows, instalar `requirements-server.txt` y definir una clave Fernet en:

`PRESENCIALIDAD_TOKEN_KEY`

Ejemplo para generar una clave:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

También se pueden suministrar:

- `PRESENCIALIDAD_GOOGLE_CLIENT_ID`
- `PRESENCIALIDAD_GOOGLE_CLIENT_SECRET`
- `PRESENCIALIDAD_PUBLIC_URL`

Para un despliegue web hay que registrar en Google el callback:

`https://TU-DOMINIO/oauth/google/callback`

## Copias de seguridad

Administración permite:

- Crear una copia inmediatamente.
- Descargar una copia SQLite.
- Restaurar una copia.
- Configurar frecuencia automática y cantidad a conservar.

Antes de una restauración se crea una copia `PreRestore` del estado actual. La restauración cierra todas las sesiones para evitar que una sesión vieja siga trabajando sobre datos reemplazados.

## Actualizar desde una base anterior

Si la versión 5.3.0 se coloca sobre una carpeta que todavía contiene `data/presencialidad.db`, intentará copiarla de forma segura al nuevo directorio de datos y ejecutar las migraciones de esquema.

También se puede indicar explícitamente:

```bash
python server.py --import-db "C:\ruta\a\presencialidad.db"
```

La importación solo funciona si todavía no existe una base en el destino. No pisa una base 5.3.0 ya existente.

## Modo servidor

Para una instalación institucional de una sola instancia:

```bash
python server.py --host 0.0.0.0 --port 8765 --no-browser --public-url https://presencialidad.example.edu
```

No se recomienda exponer directamente el puerto 8765 a Internet. Usá un proxy HTTPS como Caddy, nginx, Apache o el proxy del proveedor.

La edición 5.3.0 usa SQLite. Es adecuada para funcionamiento local y un servidor institucional de una sola instancia con disco persistente. **No está diseñada para múltiples réplicas de aplicación escribiendo simultáneamente sobre bases independientes**. Un despliegue horizontal requeriría un backend de base de datos compartida como PostgreSQL.

## Docker

Se incluyen `Dockerfile`, `docker-compose.yml` y `.env.example`.

```bash
cp .env.example .env
# editar .env
docker compose up -d --build
```

La base se guarda en el volumen `presencialidad_data`.

## Compilar un ejecutable de Windows

La carpeta incluye `Presencialidad.spec` y `BUILD_WINDOWS_EXE.bat`.

En una PC Windows:

1. Instalar Python 3.
2. Ejecutar `BUILD_WINDOWS_EXE.bat`.
3. El ejecutable queda en `dist\Presencialidad.exe`.

Después puede generarse un instalador usando `installer.iss` y Inno Setup.

El entorno donde se preparó esta entrega no genera binarios Windows de forma nativa, por lo que el ZIP incluye el proyecto listo para compilarlos en Windows en lugar de un `.exe` de procedencia dudosa vestido de instalador.

## Seguridad implementada

- Contraseñas PBKDF2-HMAC-SHA256 con salt.
- Sesiones mediante cookie HttpOnly y SameSite=Lax.
- Token CSRF para operaciones que modifican datos.
- Protección Secure cuando se ejecuta detrás de HTTPS correctamente configurado.
- Bloqueo temporal después de repetidos intentos de inicio de sesión fallidos.
- Roles validados también por el backend.
- Cierre y revocación de sesiones.
- Tokens Google protegidos con DPAPI en Windows o Fernet en servidor cuando se configura una clave.
- El archivo SQLite no se expone por HTTP.
- Encabezados CSP, X-Frame-Options, Referrer-Policy y nosniff.
- Registros de auditoría de acciones importantes.
- Integridad SQLite visible en Sistema.

## Archivos principales

- `server.py`: backend, API, SQLite, autenticación, backups y Google.
- `index.html`: interfaz.
- `styles.css`: diseño.
- `app.js`: lógica del frontend.
- `i18n.js`: traducciones.
- `manifest.webmanifest`: metadatos de la aplicación web.
- `INICIAR_APP.bat`: inicio local normal en Windows.
- `INICIAR_PORTABLE.bat`: inicio portable en Windows.
- `INICIAR_APP.sh`: inicio local en Linux/macOS.
- `Dockerfile` / `docker-compose.yml`: despliegue de servidor.
- `Presencialidad.spec`: compilación PyInstaller.
- `installer.iss`: base para instalador Inno Setup.

## Recomendación para uso real

Antes de usar datos reales de alumnos:

1. Definir quién puede ver DNI y contactos.
2. Configurar backups automáticos.
3. Probar una restauración.
4. Cambiar y documentar los permisos de cada cuenta.
5. Usar HTTPS en cualquier acceso por red.
6. Revisar las obligaciones legales de la institución sobre datos personales y menores.

Presencialidad 5.3.0 intenta resolver asistencia y seguimiento sin convertirse por entusiasmo en un ERP escolar que necesite un curso de capacitación para encontrar el botón “Presente”.
