import { useLogout } from '@/hooks/userSettingHook';

const Logout = () => {
  const logout = useLogout();
  logout();

  return (
    <div>
      <h1>Logout</h1>
    </div>
  );
};

export default Logout;