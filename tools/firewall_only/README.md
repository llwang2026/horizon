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
| `deploy_firewall_only.sh` | 一键部署（建角色 + 授权 + 打补丁 + 重启） |
| `fwaas_v2_all_tenants.patch` | 跨租户列表补丁 |
| `README.md` | 本说明 |

> 收口中间件（`FirewallOnlyMiddleware`）现已内置在 Horizon 里
> （`openstack_dashboard/contrib/firewall_only/middleware.py`，并已直接写入
> `openstack_dashboard/settings.py` 的 `MIDDLEWARE`），随 Horizon 安装即自动生效，
> **不再需要**拷贝 `middleware.py` 或修改 `local_settings` 来注册中间件。默认只有
> 被赋予 `fw_admin` 角色的用户才会被收口，其他人不受影响。若需要自定义角色名、
> 隐藏的仪表盘、面板白名单或落地页，直接在 `local_settings.py` 里覆盖对应的
> `FIREWALL_ONLY_*` 设置项即可（见下方"可选环境变量"之后的说明），无需重新部署
> 任何文件。

本目录内容围绕 fwaas 补丁与角色/用户授权，不要求 horizon 仓库在位即可运行。

## 前置条件

- 在 controller 上以 root / sudo 用户操作
- 已 `source` 管理员 `admin-openrc.sh`
- 系统已装 `patch`、`python3`，并已 pip 安装 `neutron-fwaas-dashboard`

## 部署步骤

### 1. 创建 SRE 用户并授权

脚本只做「打补丁 + 重启」，用户/角色请先按下面手动创建。

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

脚本依次执行：校验凭证 → 打补丁（自动备份、幂等）→ 重启 httpd。

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
| `MARKER_ROLE` | `fw_admin` | 收口标记角色（对应 Horizon 内置的 `FIREWALL_ONLY_ROLES` 设置，默认值一致） |
| `HTTPD_RELOAD` | `systemctl restart httpd` | 重启命令 |

```bash
SRE_USER=sre_mycloud SRE_PROJECT=mycloud bash deploy_firewall_only.sh
```

若需要自定义标记角色名、隐藏的仪表盘、面板白名单或落地页，直接在 `local_settings.py`
里覆盖 `FIREWALL_ONLY_ROLES` / `FIREWALL_ONLY_USERS` / `FIREWALL_ONLY_PANEL_SLUGS` /
`FIREWALL_ONLY_HIDDEN_DASHBOARDS` / `FIREWALL_ONLY_LANDING`（重启 httpd 生效即可，
无需重新部署任何文件）。

## 回滚

```bash
# 1) 恢复补丁（二选一）
SITE=$(python3 -c "import neutron_fwaas_dashboard,os;print(os.path.dirname(os.path.dirname(neutron_fwaas_dashboard.__file__)))")
cp -p "$SITE/neutron_fwaas_dashboard/api/fwaas_v2.py.pre-firewall-only.bak" \
      "$SITE/neutron_fwaas_dashboard/api/fwaas_v2.py"
#    cd "$SITE" && patch -R -p1 < /opt/firewall_only/fwaas_v2_all_tenants.patch
# 2) 撤销授权
openstack role remove --user sre_mycloud --user-domain Default --project mycloud --project-domain Default manager
openstack role remove --user sre_mycloud --user-domain Default --project mycloud --project-domain Default fw_admin
# 3) 重启
systemctl restart httpd
```

> 收口中间件本身是 Horizon 内置能力，不需要（也无法通过本目录）卸载；只要没有
> 任何用户持有 `fw_admin`（或 `FIREWALL_ONLY_ROLES` 里配置的角色），它就完全不生效。

## 注意事项

- 升级 `neutron-fwaas-dashboard` 后补丁会被覆盖，需重跑脚本。
- 普通（非 admin）租户完全不受影响。
- 收口中间件源码在 `openstack_dashboard/contrib/firewall_only/middleware.py`，
  随 Horizon 升级/打包自动分发，本目录不再包含它的副本。
