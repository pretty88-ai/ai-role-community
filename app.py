from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import datetime
import os
import sys
import requests

#判断是不是打包成exe
if getattr(sys,'frozen',False):
    template_folder=os.path.join(sys._MEIPASS,'templates')
    app=Flask(__name__,template_folder=template_folder)
else:
    app = Flask(__name__)
# [FIX] secret_key 改为从环境变量读取，带默认值方便本地调试
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "my_secret_2026_ai_role_dev_only")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    "DATABASE_URI", 'sqlite:///ai_role.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# [FIX] API Key 从环境变量读取，绝不硬编码
# 运行前请设置：export GLM_API_KEY="你的新key"
API_KEY = os.environ.get("GLM_API_KEY", "默认值")
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL_NAME = "glm-4-flash"
MAX_HISTORY = 20  # 发给模型的历史消息条数上限


# ========== 数据库模型 ==========
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)  # [FIX] 哈希更长


class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    role_name = db.Column(db.String(50))
    prompt = db.Column(db.Text)
    is_default = db.Column(db.Boolean, default=False)  # [FIX] 标记内置角色
    user = db.relationship('User')


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))
    is_user = db.Column(db.Boolean)
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.datetime.now)


class Diary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(100))
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.datetime.now)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(120))
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.datetime.now)
    user = db.relationship('User', backref='posts')


class PostLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))


class PostComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.datetime.now)
    user = db.relationship('User')


# ========== 工具函数 ==========
def login_required():
    return 'user_id' in session


def call_glm(role, user_msg):
    """[FIX] 带历史上下文 + 异常处理的模型调用"""
    if not API_KEY:
        raise RuntimeError("未配置 GLM_API_KEY 环境变量")

    history = (
        ChatMessage.query
        .filter_by(role_id=role.id)
        .order_by(ChatMessage.create_time)
        .all()
    )
    messages = [{"role": "system", "content": role.prompt or ""}]
    for m in history[-MAX_HISTORY:]:
        messages.append({
            "role": "user" if m.is_user else "assistant",
            "content": m.content,
        })
    messages.append({"role": "user", "content": user_msg})

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": MODEL_NAME, "messages": messages}

    res = requests.post(GLM_URL, headers=headers, json=payload, timeout=60)
    res.raise_for_status()
    data = res.json()
    return data["choices"][0]["message"]["content"]


# ========== 路由 ==========
@app.route('/')
def index():
    if not login_required():
        return redirect(url_for('login'))
    user = db.session.get(User, session['user_id'])
    # [FIX] 首页展示当前用户可见的角色（自己的 + 内置的）
    roles = Role.query.filter(
        (Role.user_id == user.id) | (Role.is_default == True)  # noqa: E712
    ).all()
    return render_template('index.html', user=user, roles=roles)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        uname = request.form['username']
        pwd = request.form['password']
        # [FIX] 密码哈希校验
        u = User.query.filter_by(username=uname).first()
        if u and check_password_hash(u.password, pwd):
            session['user_id'] = u.id
            flash('登录成功')
            return redirect(url_for('index'))
        flash('账号或密码错误')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        uname = request.form['username']
        pwd = request.form['password']
        if User.query.filter_by(username=uname).first():
            flash('用户名已存在')
            return redirect(url_for('register'))
        # [FIX] 密码哈希存储
        new_u = User(username=uname, password=generate_password_hash(pwd))
        db.session.add(new_u)
        db.session.commit()
        flash('注册成功，请登录')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))


# ========== 角色聊天 ==========
@app.route('/chat/<int:role_id>', methods=['GET', 'POST'])
def chat(role_id):
    if not login_required():
        return redirect(url_for('login'))
    role = Role.query.get_or_404(role_id)

    # [FIX] 越权校验：只能访问自己的角色或内置角色
    if role.user_id != session['user_id'] and not role.is_default:
        flash('无权访问该角色')
        return redirect(url_for('index'))

    if request.method == 'POST':
        user_msg = request.form.get('msg', '').strip()
        if not user_msg:
            flash('消息不能为空')
            return redirect(url_for('chat', role_id=role_id))

        msg_user = ChatMessage(role_id=role_id, is_user=True, content=user_msg)
        db.session.add(msg_user)
        db.session.commit()  # [FIX] 先落库，保证历史上下文完整

        try:
            ai_text = call_glm(role, user_msg)
        except Exception as e:
            # [FIX] 网络/API 异常不再导致 500
            flash(f'AI 回复失败：{e}')
            return redirect(url_for('chat', role_id=role_id))

        msg_ai = ChatMessage(role_id=role_id, is_user=False, content=ai_text)
        db.session.add(msg_ai)
        db.session.commit()
        return redirect(url_for('chat', role_id=role_id))

    msgs = (
        ChatMessage.query
        .filter_by(role_id=role_id)
        .order_by(ChatMessage.create_time)
        .all()
    )
    return render_template('chat.html', role=role, msgs=msgs)


@app.route('/role/add', methods=['GET', 'POST'])
def role_add():
    """[FIX] 新增：创建自定义角色"""
    if not login_required():
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('role_name', '').strip()
        prompt = request.form.get('prompt', '').strip()
        if not name or not prompt:
            flash('角色名和设定不能为空')
            return redirect(url_for('role_add'))
        db.session.add(Role(
            user_id=session['user_id'],
            role_name=name,
            prompt=prompt,
            is_default=False,
        ))
        db.session.commit()
        flash('角色创建成功')
        return redirect(url_for('index'))
    return render_template('role_add.html')


@app.route('/del_msg/<int:msg_id>')
def del_msg(msg_id):
    if not login_required():
        return redirect(url_for('login'))
    m = ChatMessage.query.get_or_404(msg_id)
    role = Role.query.get_or_404(m.role_id)

    # [FIX] 越权校验
    if role.user_id != session['user_id'] and not role.is_default:
        flash('无权删除该消息')
        return redirect(url_for('index'))

    rid = m.role_id  # [FIX] 先取 role_id，避免删除后访问
    db.session.delete(m)
    db.session.commit()
    flash('消息已删除')
    return redirect(url_for('chat', role_id=rid))


@app.route('/clear_chat/<int:role_id>')
def clear_chat(role_id):
    if not login_required():
        return redirect(url_for('login'))
    role = Role.query.get_or_404(role_id)

    # [FIX] 越权校验
    if role.user_id != session['user_id'] and not role.is_default:
        flash('无权操作该角色')
        return redirect(url_for('index'))

    ChatMessage.query.filter_by(role_id=role_id).delete()
    db.session.commit()
    flash('聊天记录全部清空')
    return redirect(url_for('chat', role_id=role_id))


# ========== 日记 ==========
@app.route('/diary')
def diary():
    if not login_required():
        return redirect(url_for('login'))
    diaries = (
        Diary.query
        .filter_by(user_id=session['user_id'])
        .order_by(Diary.create_time.desc())
        .all()
    )
    return render_template('diary.html', diaries=diaries)


@app.route('/diary/add', methods=['GET', 'POST'])
def diary_add():
    if not login_required():
        return redirect(url_for('login'))
    if request.method == 'POST':
        t = request.form['title']
        c = request.form['content']
        db.session.add(Diary(user_id=session['user_id'], title=t, content=c))
        db.session.commit()
        flash('日记保存成功')
        return redirect(url_for('diary'))
    return render_template('diary_add.html')


# ========== 社区 ==========
@app.route('/community')
def community():
    if not login_required():
        return redirect(url_for('login'))
    posts = Post.query.order_by(Post.create_time.desc()).all()
    return render_template('community.html', posts=posts, myid=session['user_id'])


@app.route('/post/add', methods=['GET', 'POST'])
def post_add():
    if not login_required():
        return redirect(url_for('login'))
    if request.method == 'POST':
        t = request.form['title']
        c = request.form['content']
        db.session.add(Post(user_id=session['user_id'], title=t, content=c))
        db.session.commit()
        flash('帖子发布成功！其他人可以看到啦')
        return redirect(url_for('community'))
    return render_template('post_add.html')


@app.route('/post/<int:post_id>')
def post_detail(post_id):
    if not login_required():
        return redirect(url_for('login'))
    post = Post.query.get_or_404(post_id)
    comments = (
        PostComment.query
        .filter_by(post_id=post_id)
        .order_by(PostComment.create_time)
        .all()
    )
    has_like = PostLike.query.filter_by(
        post_id=post_id, user_id=session['user_id']
    ).first()
    return render_template(
        'post_detail.html',
        post=post, comments=comments, has_like=has_like, myid=session['user_id'],
    )


@app.route('/post/like/<int:post_id>')
def post_like(post_id):
    if not login_required():
        return redirect(url_for('login'))
    exist = PostLike.query.filter_by(
        post_id=post_id, user_id=session['user_id']
    ).first()
    if exist:
        db.session.delete(exist)
    else:
        db.session.add(PostLike(post_id=post_id, user_id=session['user_id']))
    db.session.commit()
    return redirect(url_for('post_detail', post_id=post_id))


@app.route('/post/comment/<int:post_id>', methods=['POST'])
def post_comment(post_id):
    if not login_required():
        return redirect(url_for('login'))
    c = request.form.get('content', '').strip()
    if not c:  # [FIX] 过滤空评论
        flash('评论不能为空')
        return redirect(url_for('post_detail', post_id=post_id))
    db.session.add(PostComment(post_id=post_id, user_id=session['user_id'], content=c))
    db.session.commit()
    flash('评论成功')
    return redirect(url_for('post_detail', post_id=post_id))


@app.route('/post/del/<int:post_id>')
def post_del(post_id):
    if not login_required():
        return redirect(url_for('login'))
    p = Post.query.get_or_404(post_id)
    if p.user_id != session['user_id']:
        flash('无权删除别人帖子')
        return redirect(url_for('community'))
    PostLike.query.filter_by(post_id=post_id).delete()
    PostComment.query.filter_by(post_id=post_id).delete()
    db.session.delete(p)
    db.session.commit()
    flash('帖子已删除')
    return redirect(url_for('community'))


# 测试路由
@app.route('/test')
def test_page():
    return "<h1 style='color:#7b61b4'>✅ 服务正常！</h1>"


# ========== 初始化：建表 + 内置角色 ==========
def init_db():
    with app.app_context():
        db.create_all()
        # [FIX] 幂等 seed 几个内置角色，避免首页空白
        default_roles = [
            {
                "role_name": "温柔倾听者",
                "prompt": "你是一位温柔、有耐心的倾听者，用温和的语气回应，先共情再给建议。",
            },
            {
                "role_name": "毒舌吐槽君",
                "prompt": "你是一个毒舌但有趣的吐槽朋友，说话犀利幽默，但不会人身攻击。",
            },
            {
                "role_name": "英语学习搭子",
                "prompt": "你是英语陪练，用户说中文时你用英文回复并给出简单纠正，鼓励用户多开口。",
            },
        ]
        for r in default_roles:
            exists = Role.query.filter_by(
                role_name=r["role_name"], is_default=True
            ).first()
            if not exists:
                db.session.add(Role(
                    user_id=None,
                    role_name=r["role_name"],
                    prompt=r["prompt"],
                    is_default=True,
                ))
        db.session.commit()


if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
