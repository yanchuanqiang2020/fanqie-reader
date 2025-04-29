# backend/auth.py
from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from models import db, User  #

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.post("/register")
def register():
    data = request.json or {}
    if User.query.filter_by(username=data["username"]).first():  #
        return jsonify(msg="用户已存在"), 400
    u = User(username=data["username"])  #
    u.set_password(data["password"])  #
    db.session.add(u)  #
    db.session.commit()  #
    return jsonify(msg="注册成功")


@auth_bp.post("/login")
def login():
    data = request.json or {}
    u = User.query.filter_by(username=data["username"]).first()  #
    if not (u and u.check_password(data["password"])):  #
        return jsonify(msg="用户名或密码错误"), 401
    token = create_access_token(identity=str(u.id))  #
    return jsonify(access_token=token)


@auth_bp.get("/me")
@jwt_required()
def me():
    uid = get_jwt_identity()
    # Attempt to get user by ID. Consider converting uid if it's a string
    # user = User.query.get(int(uid)) # If uid is always an int string
    user = User.query.get(uid)  #

    # --- MODIFICATION START ---
    # Check if the user was found in the database
    if user is None:
        # Return 404 Not Found if user corresponding to token doesn't exist
        return jsonify(msg="用户不存在或令牌无效"), 401
    # --- MODIFICATION END ---

    # If user exists, return user details
    return jsonify(id=user.id, username=user.username)  #
