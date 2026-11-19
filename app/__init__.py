import os

from flask import Flask


def create_app(config=None):
    app = Flask(__name__)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "APP_DATABASE", os.path.join(base_dir, "data", "app.db")
        ),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 单个 CSV 上限 16MB
    )
    if config:
        app.config.update(config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    from . import db, routes, logic

    db.init_app(app)
    app.register_blueprint(routes.bp)

    app.add_template_filter(logic.fmt_num, "fmt_num")
    app.add_template_filter(logic.fmt_ts, "fmt_ts")

    return app
