import { useEffect, useState } from 'react';
import { motion } from 'motion/react';
import { toast } from 'sonner';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';

interface UserStats {
  total_users: number;
  admin_count: number;
  user_count: number;
  active_users: number;
  disabled_users: number;
}

interface UserRow {
  id: string;
  email: string;
  name: string;
  role: string;
  status: string;
  created_at: string;
}

export function AdminPanel() {
  const [stats, setStats] = useState<UserStats | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState<string | null>(null);

  const fetchStats = async () => {
    const response = await authFetch(`${config.mainApiUrl}/admin/users/stats`);
    if (!response.ok) {
      throw new Error('获取统计失败');
    }
    const data = await response.json();
    setStats(data);
  };

  const fetchUsers = async () => {
    const response = await authFetch(`${config.mainApiUrl}/admin/users`);
    if (!response.ok) {
      throw new Error('获取用户列表失败');
    }
    const data = await response.json();
    setUsers(Array.isArray(data) ? data : []);
  };

  const refresh = async () => {
    setLoading(true);
    try {
      await Promise.all([fetchStats(), fetchUsers()]);
    } catch (error) {
      const message = error instanceof Error ? error.message : '加载失败';
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const handleToggleStatus = async (user: UserRow) => {
    const nextStatus = user.status === 'active' ? 'suspended' : 'active';
    setUpdating(user.id);
    try {
      const response = await authFetch(`${config.mainApiUrl}/admin/users/${user.id}/status`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ status: nextStatus }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail?.detail || '更新失败');
      }
      setUsers((prev) =>
        prev.map((row) => (row.id === user.id ? { ...row, status: nextStatus } : row))
      );
      toast.success(nextStatus === 'active' ? '已启用用户' : '已禁用用户');
      await fetchStats();
    } catch (error) {
      const message = error instanceof Error ? error.message : '更新失败';
      toast.error(message);
    } finally {
      setUpdating(null);
    }
  };

  return (
    <div className="space-y-6">
      <motion.div
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-[#e8eaed] text-lg">管理员控制台</h3>
            <p className="text-[#94a3b8] text-sm">用户统计与账号管理</p>
          </div>
          <button
            onClick={refresh}
            className="px-4 py-2 rounded-lg text-sm bg-[rgba(0,212,255,0.12)] text-[#00d4ff] hover:bg-[rgba(0,212,255,0.2)] transition-colors"
          >
            刷新数据
          </button>
        </div>
      </motion.div>

      <div className="grid grid-cols-5 gap-4">
        {[
          { label: '用户总数', value: stats?.total_users ?? '--' },
          { label: '管理员', value: stats?.admin_count ?? '--' },
          { label: '普通用户', value: stats?.user_count ?? '--' },
          { label: '活跃用户', value: stats?.active_users ?? '--' },
          { label: '已禁用', value: stats?.disabled_users ?? '--' },
        ].map((item, index) => (
          <motion.div
            key={item.label}
            className="glass gradient-border rounded-2xl p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.05 }}
          >
            <div className="text-[#94a3b8] text-xs">{item.label}</div>
            <div className="text-[#e8eaed] text-2xl mt-2">{item.value}</div>
          </motion.div>
        ))}
      </div>

      <motion.div
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex items-center justify-between mb-4">
          <h4 className="text-[#e8eaed]">用户列表</h4>
          {loading && <span className="text-xs text-[#94a3b8]">加载中...</span>}
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-[#94a3b8] border-b border-[rgba(0,212,255,0.15)]">
                <th className="py-3">邮箱</th>
                <th className="py-3">名称</th>
                <th className="py-3">角色</th>
                <th className="py-3">状态</th>
                <th className="py-3">注册时间</th>
                <th className="py-3 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="text-[#e8eaed]">
              {users.map((user) => (
                <tr
                  key={user.id}
                  className="border-b border-[rgba(0,212,255,0.1)] hover:bg-[rgba(0,212,255,0.05)] transition-colors"
                >
                  <td className="py-3">{user.email}</td>
                  <td className="py-3">{user.name || '-'}</td>
                  <td className="py-3">
                    <span className="px-2 py-1 rounded-full text-xs bg-[rgba(0,212,255,0.1)] text-[#00d4ff]">
                      {user.role}
                    </span>
                  </td>
                  <td className="py-3">
                    <span
                      className={`px-2 py-1 rounded-full text-xs ${
                        user.status === 'active'
                          ? 'bg-[rgba(0,255,136,0.15)] text-[#00ff88]'
                          : 'bg-[rgba(255,59,92,0.15)] text-[#ff3b5c]'
                      }`}
                    >
                      {user.status === 'active' ? '活跃' : '已禁用'}
                    </span>
                  </td>
                  <td className="py-3 text-[#94a3b8]">{user.created_at || '-'}</td>
                  <td className="py-3 text-right">
                    <button
                      onClick={() => handleToggleStatus(user)}
                      disabled={updating === user.id}
                      className={`px-3 py-1 rounded-lg text-xs transition-colors ${
                        user.status === 'active'
                          ? 'bg-[rgba(255,59,92,0.15)] text-[#ff3b5c] hover:bg-[rgba(255,59,92,0.25)]'
                          : 'bg-[rgba(0,255,136,0.15)] text-[#00ff88] hover:bg-[rgba(0,255,136,0.25)]'
                      }`}
                    >
                      {updating === user.id ? '处理中...' : user.status === 'active' ? '禁用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
              {!users.length && !loading && (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-[#94a3b8]">
                    暂无用户
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </motion.div>
    </div>
  );
}
