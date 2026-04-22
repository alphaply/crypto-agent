import React, { useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, Typography } from 'antd';

const { Title, Paragraph } = Typography;

export default function AuthGate({ authenticated, loading, onLogin, children }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const formInitialValues = useMemo(() => ({ password: '' }), []);

  const handleSubmit = async (values) => {
    setSubmitting(true);
    setError('');
    try {
      await onLogin(values.password);
    } catch (err) {
      setError(err.message || 'Login failed');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return null;
  }

  if (authenticated) {
    return children;
  }

  return (
    <div className="login-screen">
      <Card className="glass-card login-panel">
        <Title level={2} style={{ marginTop: 0 }}>
          Crypto Agent Console
        </Title>
        <Paragraph type="secondary">
          新前后分离版本使用 JWT 登录。输入当前项目的管理密码后即可访问管理端、历史、聊天和配置页面。
        </Paragraph>
        {error ? <Alert style={{ marginBottom: 16 }} type="error" message={error} showIcon /> : null}
        <Form layout="vertical" initialValues={formInitialValues} onFinish={handleSubmit}>
          <Form.Item label="Password" name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password placeholder="ADMIN_PASSWORD / CHAT_PASSWORD" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
