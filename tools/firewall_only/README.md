# 防火墙专属 SRE 部署

## 这是什么

给低权限 SRE 账号一个 **只能看防火墙** 的 Horizon 界面：

- 登录后只显示防火墙模块，其它资源（网络 / 路由 / 实例 / 卷 / Admin / Identity / 设置）全部隐藏
- 误点到其它面板会自动跳回防火墙页，不会看到「无权限」报错
- 以所绑定项目的 `manager` 身份管理该项目的防火墙

> 本方案下 SRE = 已有项目的 `manager`（项目内管理）+ `fw_admin`（UI 收口标记，无 API 权限）。`manager` 不是 `admin`，因此**无法跨租户看全部防火墙**，只能看自己所绑定项目的防火墙。若要跨租户，仍需给 `admin` 角色（见注意事项）。

## 目录内容

| 文件 | 说明 |
|------|------|
| `deploy_firewall_only.sh` | 一键部署（建角色 + 授权 + 放中间件 + 打补丁 + 改配置 + 重启） |
| `middleware.py` | 收口中间件源码（随 bundle 附带） |
| `fwaas_v2_all_tenants.patch` | 跨租户列表补丁 |
| `README.md` | 本说明 |

本目录**自包含**：拷到 controller 任意位置即可运行，不要求 horizon 仓库在位。

## 前置条件

- 在 controller 上以 root / sudo 用户操作
- 已 `source` 管理员 `admin-openrc.sh`
- 系统已装 `patch`、`python3`，并已 pip 安装 `neutron-fwaas-dashboard`

## 部署步骤

### 1. 创建 SRE 用户并授权

脚本只做「放中间件 + 打补丁 + 改配置 + 重启」，用户/角色请先按下面手动创建。

思路：**查一个已存在的项目 → 建一个 `sre_<项目名>` 用户 → 在该项目上授予 `manager` 角色 + `fw_admin` 标记角色。**

```bash
export SRE_DOMAIN=Default
export SRE_PROJECT=mycloud            # ① 已存在的项目名称（按需修改）
export SRE_PASS='Sre@Passw0rd'

# ② 用户名为 sre_ + 项目名称
export SRE_USER="sre_${SRE_PROJECT}"

# 确认项目存在（不存在会报错，请先建好或用正确名称）
openstack project show --domain "$SRE_DOMAIN" "$SRE_PROJECT" >/dev/null

# ③ 创建 SRE 用户（已存在会报错，可忽略）
openstack user create --domain "$SRE_DOMAIN" --password "$SRE_PASS" "$SRE_USER" 2>/dev/null

# ④ 创建 UI 收口标记角色（无 API 权限，仅作标记）
openstack role create fw_admin 2>/dev/null

# ⑤ 赋予项目 manager 角色（项目内管理权限）
openstack role add --user "$SRE_USER" --user-domain "$SRE_DOMAIN" \
    --project "$SRE_PROJECT" --project-domain "$SRE_DOMAIN" manager

# ⑥ 赋予 fw_admin 标记角色
openstack role add --user "$SRE_USER" --user-domain "$SRE_DOMAIN" \
    --project "$SRE_PROJECT" --project-domain "$SRE_DOMAIN" fw_admin

echo "SRE 用户 '$SRE_USER' 已在项目 '$SRE_PROJECT' 授权（manager + fw_admin）"
```

> `manager` 给项目内管理权限（非 `admin`，无跨租户能力）；`fw_admin` 仅作中间件识别标记，本身无任何 API 权限。

### 2. 运行一键部署

```bash
source ~/admin-openrc.sh
cd /opt/firewall_only          # 本目录拷贝到 controller 的位置
SRE_USER=sre_mycloud SRE_PROJECT=mycloud bash deploy_firewall_only.sh
```

脚本依次执行：校验凭证 → 放中间件 → 打补丁（自动备份、幂等）→ 改 `local_settings`（幂等）→ 重启 httpd。

### 3. 验证

- 用 SRE 登录 → 只看到防火墙，且列表展示**该项目**的防火墙
- 误触其它面板 URL → 自动跳回防火墙页
- 用真正的 admin 登录 → 完全不受影响

## 可选环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SRE_USER` | `sre_<项目名>` | SRE 用户名（建议为 `sre_` + 项目名） |
| `SRE_PROJECT` | （必填） | 已存在的项目名称 |
| `SRE_DOMAIN` | `Default` | 域 |
| `MARKER_ROLE` | `fw_admin` | 收口标记角色 |
| `HORIZON_LOCAL` | `/usr/share/openstack-dashboard/openstack_dashboard/local` | 中间件目录 |
| `LOCAL_SETTINGS` | `/etc/openstack-dashboard/local_settings` | Horizon 配置 |
| `HTTPD_RELOAD` | `systemctl restart httpd` | 重启命令 |

非标准安装（如 devstack）才需覆盖路径，**注意指向运行时加载路径，不是 git 仓库源码目录**：

```bash
SRE_USER=sre_mycloud SRE_PROJECT=mycloud \
  HORIZON_LOCAL=/opt/stack/horizon/openstack_dashboard/local \
  LOCAL_SETTINGS=/etc/openstack-dashboard/local_settings.py \
  bash deploy_firewall_only.sh
```

## 回滚

```bash
# 1) 从 local_settings 删除标记块：# --- Firewall-only SRE restriction (Plan A)
# 2) 删除中间件
rm -f /usr/share/openstack-dashboard/openstack_dashboard/local/middleware.py
# 3) 恢复补丁（二选一）
SITE=$(python3 -c "import neutron_fwaas_dashboard,os;print(os.path.dirname(os.path.dirname(neutron_fwaas_dashboard.__file__)))")
cp -p "$SITE/neutron_fwaas_dashboard/api/fwaas_v2.py.pre-firewall-only.bak" \
      "$SITE/neutron_fwaas_dashboard/api/fwaas_v2.py"
#    cd "$SITE" && patch -R -p1 < /opt/firewall_only/fwaas_v2_all_tenants.patch
# 4) 撤销授权
openstack role remove --user sre_mycloud --user-domain Default --project mycloud --project-domain Default manager
openstack role remove --user sre_mycloud --user-domain Default --project mycloud --project-domain Default fw_admin
# 5) 重启
systemctl restart httpd
```

## 注意事项

- 升级 `neutron-fwaas-dashboard` 后补丁会被覆盖，需重跑脚本。
- 普通（非 admin）租户完全不受影响。
- bundle 里的 `middleware.py` 是拷贝，canonical 源在 `openstack_dashboard/local/middleware.py`；改了源文件记得同步：
  ```bash
  cp -p openstack_dashboard/local/middleware.py tools/firewall_only/middleware.py
  ```
