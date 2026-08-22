# Порядок импортов задаёт порядок регистрации обработчиков — не менять:
# перехватчик рассылки и команды должны встать раньше остальных.
from . import commands    # noqa: F401
from . import user        # noqa: F401
from . import callbacks   # noqa: F401
from . import admin       # noqa: F401
